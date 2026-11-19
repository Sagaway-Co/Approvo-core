"""Slack Block Kit 渲染 + 发送。对应飞书侧的 app/cards.py(数据同源,只是换了渲染)。

- approval_message:审批频道里的那条审批消息(待审批 → 已批准·部署中 → 已部署/失败,或已拒绝)
- deploying_message / result_message:结果频道的「部署中」「结果 / 被拒」通知

🔴 一切来自外部的文本(notes / PR 标题 / 提交信息 / 仓库名 / 申请人名 / 部署日志)都必须过 esc():
   Slack mrkdwn 里 `<!channel>` 会 @ 全频道、`<https://evil|看起来正规的链接>` 能伪装链接。
   notes 是 CI 随请求传来的自由文本 —— 不转义,等于让任何拿到 RELEASE_TOKEN 的人
   往审批频道里注入提及与钓鱼链接,而审批频道恰恰是"有权批生产的人"聚集的地方。
   不需要转义的只有我们自己校验过格式的 ID(频道 / 用户),它们以 <#C…> / <@U…> 形式出现。
"""
from app import settings
from app.cards import _env_cn, _is_prod, _title_prefix
from app.providers.slack import client

APPROVE_ACTION = "approvo_approve"
REJECT_ACTION = "approvo_reject"
DECISION_BLOCK = "approvo_decision"

# Block Kit 上限:section text 3000、fields 每项 2000 且最多 10 项、header 150、
# confirm.text 300。留余量给截断提示 —— 超限时 Slack 直接拒绝整条消息(invalid_blocks),
# 那等于审批发不出去,所以宁可截断也不能超。
_TEXT_MAX = 2900
_FIELD_MAX = 1900
_LOG_TAIL = 1800


def esc(s) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _code(s) -> str:
    """行内代码。反引号会提前闭合代码段,换成单引号。"""
    t = esc(s).replace("`", "'")
    return f"`{t}`" if t else "-"


def _clip(t: str, n: int) -> str:
    return t if len(t) <= n else t[: n - 1] + "…"


def _link(url, text: str) -> str:
    """只给形状干净的 https 链接生成 <url|text>;否则退化为纯文本,不冒险拼出一个可点的东西。"""
    u = str(url or "")
    if u.startswith("https://") and not any(c in u for c in "<>| \n"):
        return f"<{u}|{esc(text)}>"
    return esc(text)


def _uid(u) -> str | None:
    """只有格式合法的 user ID 才渲染成 <@…>(它不经 esc,所以必须先验形状)。"""
    s = str(u or "")
    return s if settings.SLACK_USER_ID_RE.fullmatch(s) else None


def _header(text: str) -> dict:
    # plain_text 不解析 mrkdwn,不存在注入,无需 esc
    return {"type": "header", "text": {"type": "plain_text", "text": _clip(text, 150), "emoji": True}}


def _section(md: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _clip(md, _TEXT_MAX)}}


def _fields(pairs: list) -> dict:
    return {"type": "section", "fields": [
        {"type": "mrkdwn", "text": _clip(f"*{k}*\n{v}", _FIELD_MAX)} for k, v in pairs[:10]]}


def _context(md: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _clip(md, _TEXT_MAX)}]}


def _prod_fence() -> dict:
    return _context("⚠️" * 12 + " *生产发版* " + "⚠️" * 12)


# ─────────────────── 各区块(与 cards.py 同语义) ───────────────────

def _applicant(spec: dict) -> str:
    """申请人:能映射到 Slack 用户就 @(Slack 会渲染成真名),否则明示「未映射」。"""
    ids = [u for u in (_uid(x) for x in spec.get("slack_applicant_ids") or []) if u]
    gh = spec.get("operator_github") or ""
    name = spec.get("operator_name") or ""
    if ids:
        out = " ".join(f"<@{u}>" for u in ids)
        if spec.get("operator_mapped") and name:
            out += f"({esc(name)})"
        if gh:
            out += f"\n{_code(gh)}"
        return out
    who = _code(gh) if gh else esc(name or "-")
    return f"{who}\n⚠️ 未映射到 Slack 用户"


def _applicant_short(spec: dict) -> str:
    uid = _uid(spec.get("operator_id"))
    if uid:
        return f"<@{uid}>"
    gh = spec.get("operator_github")
    return _code(gh) if gh else esc(spec.get("operator_name") or "-")


def _approver(spec: dict, approver_name) -> str:
    a = spec.get("slack_approver") or {}
    uid = _uid(a.get("id"))
    if uid:
        return f"<@{uid}>" + (f"({esc(a['name'])})" if a.get("name") else "")
    return esc(approver_name or "-")   # 审批直通等非人工路径:原样显示来源说明


def _target(spec: dict) -> str:
    """真实部署目标。method=github 时取 releases.*.deploy —— 与 server 里审批表单同一判据:
    缺了就明说缺了,不让审批人对着一个仓库名以为那是集群。"""
    method = spec.get("method")
    if method == "github":
        d = spec.get("deploy") or {}
        if d.get("cluster") and d.get("namespace"):
            return f"{_code(d['cluster'])} / {_code(d['namespace'])}"
        return "⚠️ 部署目标缺失(releases.*.deploy 未配置)"
    if method == "dryrun":
        return "dryrun(不连集群)"
    if spec.get("cluster") or spec.get("namespace"):
        return f"{_code(spec.get('cluster'))} / {_code(spec.get('namespace'))}"
    return "-"


def _image(spec: dict) -> str:
    img = spec.get("image") or ""
    if img == spec.get("tag"):
        return "*镜像*\n本次不改镜像(清单应用,镜像沿用集群现值)"
    return f"*镜像*\n{_code(img)}"


def _env_change(spec: dict) -> str:
    """固定显示(语义同 cards._env_change_block):让审批人分得清「没有变更」和「有变更没申报」。
    Slack 支持 ``` 代码块(飞书 lark_md 不支持),diff 原样对齐显示。"""
    notes = (spec.get("notes") or "").strip()
    if not notes:
        return "*🔧 环境变更*\n仅镜像变更 —— 本次申请未附带环境变量/配置变更"
    max_lines = 40
    lines = notes.splitlines()
    body = esc("\n".join(ln.rstrip() for ln in lines[:max_lines])).replace("```", "'''")
    more = f"\n… 共 {len(lines)} 行,完整内容见流水线日志" if len(lines) > max_lines else ""
    return f"*🔧 环境变更*\n```{_clip(body, _TEXT_MAX - 200)}```{more}"


def _subject(spec: dict) -> str:
    """一句话说清批的是什么:仓库 + 版本(通知标题、线程回复用)。"""
    return f"{spec['repo']} {spec['tag']}"


def _db_change(spec: dict) -> str | None:
    """语义同 cards._db_change_block:没配 db_migrations 不渲染;查不到绝不渲染成「无变更」。"""
    d = spec.get("db_changes")
    if d is None:
        return None
    st = (d or {}).get("status")
    if st == "ok" and d.get("files"):
        rows = []
        for f in d["files"][:15]:
            mark = {"added": "新增", "modified": "修改", "removed": "删除"}.get(
                f.get("status"), esc(f.get("status") or ""))
            danger = ("  🔴 " + " / ".join(esc(x) for x in f["danger"])) if f.get("danger") else ""
            rows.append(f"{_code(str(f.get('name'))[:80])} {mark}{danger}")
        more = f"\n… 共 {len(d['files'])} 个文件" if len(d["files"]) > 15 else ""
        note = ("\n⚠️ 口径:自本应用上次成功部署以来的变更(时间口径);"
                "实际会执行哪些以迁移门禁为准。")
        warn = ("\n🔴 含破坏性语句 —— 镜像可回滚、schema 不可回滚,"
                "回滚镜像只会让旧代码撞上已变的表结构。"
                if any(f.get("danger") for f in d["files"]) else "")
        return "*🗄 数据库变更*\n" + "\n".join(rows) + more + note + warn
    if st == "none":
        return "*🗄 数据库变更*\n无 —— 自上次成功部署以来迁移目录没有变更"
    reason = (d or {}).get("reason") or "未查询"
    return (f"*🗄 数据库变更*\n⚠️ 无法判定({esc(str(reason)[:100])})"
            f" —— 请在批准前自行确认本次是否含迁移")


def _changes(spec: dict) -> str | None:
    """「本次变更」:语义同 cards._changes_block(数据来自 server._collect_changes)。"""
    ch = spec.get("changes")
    if not ch:
        return None
    st = ch.get("status")
    if st == "first":
        return "*📦 本次变更*\n首次经网关记录发版,无上一版本可对比"
    if st == "error":
        rng = f"{ch.get('base') or '?'}...{(ch.get('head') or '?')[:12]}"
        return (f"*📦 本次变更*\n⚠️ 变更清单获取失败,请人工核对(不影响审批)\n"
                f"· 对比区间: {_code(rng)}\n· 错误: {_code(ch.get('error') or '未知错误')}")
    if st == "nodiff":
        return "*📦 本次变更*\n与已部署版本相比无新增提交(重发或回退)"
    prs, direct = ch.get("prs") or [], ch.get("direct_commits") or []
    lines = [f"*📦 本次变更:{len(prs)} 个 PR · 共 {ch.get('total_commits', 0)} 个提交*"]
    for p in prs[:10]:
        lines.append(f"· {_link(p.get('url'), '#' + str(p.get('number')))} "
                     f"{esc(p.get('title'))} — {esc(p.get('author') or '?')}")
    if len(prs) > 10:
        lines.append(f"· …共 {len(prs)} 个 PR,其余见完整对比")
    if direct:
        lines.append(f"*⚠️ 未关联 PR 的直接提交:{len(direct)} 个*")
        for c in direct[:5]:
            lines.append(f"· {_code(c.get('sha'))} {esc(c.get('message'))} — {esc(c.get('author') or '?')}")
        if len(direct) > 5:
            lines.append(f"· …等 {len(direct)} 个")
    if ch.get("compare_url"):
        lines.append(_link(ch["compare_url"], "查看完整代码对比"))
    return "\n".join(lines)


def _policy(spec: dict) -> str:
    """把「谁能批」写在消息上:规则不可见,被拒的人只会以为是机器人坏了。"""
    parts = [f"审批人 = <#{settings.SLACK_APPROVAL_CHANNEL}> 的当前成员(或签,任一通过即部署)"]
    allow = settings.slack_approvers_for(spec.get("stage"))
    if allow:
        parts.append("本 stage 仅限 " + " ".join(f"<@{u}>" for u in sorted(allow) if _uid(u)))
    if settings.SLACK_FORBID_SELF_APPROVAL:
        parts.append("禁止自批")
        if not spec.get("slack_applicant_ids"):
            parts.append("⚠️ 申请人未映射:仅 slack.user_map 中登记、且不是该 GitHub 账号的成员可批准"
                         if spec.get("operator_github") else
                         "⚠️ 申请人身份未知:无法确认任何人不是申请人,请拒绝后补全身份重新提交")
    return " · ".join(parts)


def _single_message() -> bool:
    """单卡模式:结果频道就是审批频道(没另配 result_channel)时,不再另发「部署中 / 结果 / 被拒」消息,
    只更新审批那一条 —— 同一个频道里三条消息说的是同一件事,只会把状态冲散。
    另配了结果频道时保持原行为(那是有人专门订阅结果的地方)。"""
    return settings.SLACK_RESULT_CHANNEL == settings.SLACK_APPROVAL_CHANNEL


def _hm(when, ref) -> str:
    """'YYYY-MM-DD HH:MM' → 同一天只显示 HH:MM,跨天带日期。"""
    w, r = str(when or ""), str(ref or "")
    if len(w) == 16 and w[:10] == r[:10]:
        return w[11:]
    return w or "-"


def _timeline(spec: dict, dec: dict) -> str:
    """进度时间线:发起 → 批准/拒绝 → 部署中 → 完成。消息只保留最新状态,经过的每一步在这一行里留时间。"""
    submit = spec.get("submit_time") or ""
    uid = _uid(dec.get("user"))
    who = (f"<@{uid}>" if uid else "-") + (f"({esc(dec['name'])})" if dec.get("name") else "")
    steps = [f"{_code(_hm(submit, submit))} 发起"]
    if dec.get("kind") == "rejected":
        steps.append(f"{_code(_hm(dec.get('when'), submit))} {who} 🚫 拒绝")
        return "*🕒 进度* " + " → ".join(steps)
    steps.append(f"{_code(_hm(dec.get('when'), submit))} {who} 批准")
    final = dec.get("final")
    if dec.get("deploying_at"):
        steps.append(f"{_code(_hm(dec['deploying_at'], submit))} 部署中")
    if final in ("success", "failed"):
        mark = "✅ 已部署" if final == "success" else "❌ 部署失败"
        steps.append(f"{_code(_hm(dec.get('finished_at') or '', submit))} {mark}")
    elif final not in (None, "deploying"):
        steps.append(f"状态:{esc(final)}")
    else:
        steps.append("…")
    return "*🕒 进度* " + " → ".join(steps)


def _buttons(instance_code: str, spec: dict) -> dict:
    """批准(primary)/ 拒绝(danger),都带 confirm 弹窗 —— 一次误触就是一次生产发版。"""
    is_prod = _is_prod(spec)
    what = f"*{esc(spec['repo'])}* {_code(spec['tag'])}" + (" 到 *生产*" if is_prod else "")
    return {"type": "actions", "block_id": DECISION_BLOCK, "elements": [
        {"type": "button", "action_id": APPROVE_ACTION, "style": "primary", "value": instance_code,
         "text": {"type": "plain_text", "text": "✅ 批准", "emoji": True},
         "confirm": {"title": {"type": "plain_text", "text": "确认批准并部署?"},
                     "text": {"type": "mrkdwn",
                              "text": _clip(f"批准后将立即部署 {what},不可撤回。你的身份会被记录。", 300)},
                     "confirm": {"type": "plain_text", "text": "批准并部署"},
                     "deny": {"type": "plain_text", "text": "取消"},
                     "style": "primary"}},
        {"type": "button", "action_id": REJECT_ACTION, "style": "danger", "value": instance_code,
         "text": {"type": "plain_text", "text": "🚫 拒绝", "emoji": True},
         "confirm": {"title": {"type": "plain_text", "text": "确认拒绝?"},
                     "text": {"type": "mrkdwn",
                              "text": _clip(f"拒绝后 {what} 本次申请作废,需重新提交才能再发。", 300)},
                     "confirm": {"type": "plain_text", "text": "拒绝"},
                     "deny": {"type": "plain_text", "text": "取消"},
                     "style": "danger"}},
    ]}


# ─────────────────── 消息 ───────────────────

def approval_message(instance_code: str, spec: dict, decision: dict | None = None) -> tuple[str, list]:
    """审批消息。decision=None → 待审批(带按钮);否则 → 去掉按钮、写明谁何时批准/拒绝。

    决策后用同一函数重渲染整条消息(而不是只删按钮):审批频道里留下的永远是
    "批的是什么 + 谁批的"的完整快照,事后翻历史不必再去对结果频道。
    """
    is_prod = _is_prod(spec)
    prefix = _title_prefix(spec)
    kind = "生产发版申请" if is_prod else "发版申请"
    if decision is None:
        title = f"🚀 {prefix}{kind} · 待审批"
    elif decision.get("kind") == "rejected":
        title = f"🚫 {prefix}{kind} · 已拒绝"
    else:
        title = {"success": f"✅ {prefix}{kind} · 已部署",
                 "failed": f"❌ {prefix}{kind} · 部署失败"}.get(
            decision.get("final"), f"🚀 {prefix}{kind} · 已批准 · 部署中…")

    blocks: list = [_header(title), _fields([
        ("项目", esc(spec.get("project") or "-")),
        ("应用", esc(spec["repo"])),
        ("版本", _code(spec["tag"])),
        ("环境", esc(_env_cn(spec.get("env")))),
        ("Stage", esc(spec.get("stage") or "-")),
        ("部署平台", esc(spec.get("platform") or "-")),
        ("部署到", _target(spec)),
        ("申请人", _applicant(spec)),
        ("申请时间", esc(spec.get("submit_time") or "-")),
    ]), _section(_image(spec)), _section(_env_change(spec))]
    for extra in (_db_change(spec), _changes(spec)):
        if extra:
            blocks.append(_section(extra))
    if decision is None:
        blocks += [_context(_policy(spec)), _buttons(instance_code, spec)]
    else:
        blocks.append(_section(_timeline(spec, decision)))
        if decision.get("final") == "failed" and decision.get("log"):
            tail = esc(str(decision["log"])[-_LOG_TAIL:]).replace("```", "'''")
            blocks.append(_section(f"*失败详情*\n```{tail}```"))
    blocks.append(_context(f"审批单号 {_code(instance_code)}"))
    if is_prod:
        blocks = [_prod_fence(), *blocks, _prod_fence()]
    return f"{title}:{_subject(spec)}", blocks


def _result_fields(spec: dict, approver_name, info: str, when) -> dict:
    return _fields([
        ("应用", esc(spec["repo"])),
        ("环境", esc(_env_cn(spec.get("env")))),
        ("部署平台", esc(spec.get("platform") or "-")),
        ("版本", _code(spec["tag"])),
        ("申请人", _applicant_short(spec)),
        ("审批人", _approver(spec, approver_name)),
        ("审批信息", info),
        ("时间", esc(when or "-")),
    ])


def deploying_message(spec: dict, approver_name, when) -> tuple[str, list]:
    prefix = _title_prefix(spec)
    title = f"🚀 {prefix}生产部署中…" if _is_prod(spec) else f"🚀 {prefix}已审批 · 部署中…"
    blocks = [_header(title), _result_fields(spec, approver_name, "审批通过,部署中", when),
              _section(_image(spec)), _section(_env_change(spec))]
    db = _db_change(spec)
    if db:
        blocks.append(_section(db))
    return f"{title}:{_subject(spec)}", blocks


def result_message(spec: dict, ok: bool = True, rejected: bool = False, approver_name=None,
                   when=None, log: str | None = None) -> tuple[str, list]:
    """结果通知。成功附变更清单;失败附日志尾部;被拒只写谁拒的。"""
    prefix = _title_prefix(spec)
    prod = "生产" if _is_prod(spec) else ""
    if rejected:
        title, info = f"🚫 {prefix}{prod}发版被拒绝", "已拒绝"
    elif ok:
        title, info = f"✅ {prefix}{prod}版本已部署", "审批通过"
    else:
        title, info = f"❌ {prefix}{prod}部署失败", "审批通过(部署失败)"
    blocks = [_header(title), _result_fields(spec, approver_name, info, when), _section(_image(spec))]
    if not rejected:
        blocks.append(_section(_env_change(spec)))
        db = _db_change(spec)
        if db:
            blocks.append(_section(db))
    if ok and not rejected:
        ch = _changes(spec)
        if ch:
            blocks.append(_section(ch))
    if not rejected and not ok and log:
        tail = esc(log[-_LOG_TAIL:]).replace("```", "'''")
        blocks.append(_section(f"*失败详情*\n```{tail}```"))
    return f"{title}:{_subject(spec)}", blocks


# ─────────────────── 发送 ───────────────────

def post_approval(instance_code: str, spec: dict) -> tuple[str, str]:
    """发审批消息。🔴 失败照常抛 —— 由 /release 置 failed 并返回 502(不留发不出去的 pending)。"""
    text, blocks = approval_message(instance_code, spec)
    return client.post_message(settings.SLACK_APPROVAL_CHANNEL, text, blocks)


def update_approval(channel: str, ts: str, instance_code: str, spec: dict, decision: dict) -> None:
    """把审批消息改成「已决策」形态。失败只记日志:消息是展示,状态以 store 为准。"""
    if not (channel and ts):
        print(f"[slack][warn] 缺少原消息定位(channel/ts),跳过更新 instance={instance_code}")
        return
    try:
        text, blocks = approval_message(instance_code, spec, decision)
        client.update_message(channel, ts, text, blocks)
    except Exception as e:  # 展示失败绝不能影响状态与部署(BLE001 已全局 ignore)
        print(f"[slack][warn] 更新审批消息失败(不影响状态): {type(e).__name__}: {e}")


def _safe_post_result(text: str, blocks: list, what: str) -> None:
    """同 events._safe_card:通知是辅助,绝不能让它的失败阻断部署这个主流程。"""
    try:
        client.post_message(settings.SLACK_RESULT_CHANNEL, text, blocks)
    except Exception as e:
        print(f"[slack][warn] 发送{what}通知失败(不影响部署): {type(e).__name__}: {e}")


def _card_ref(spec: dict) -> dict | None:
    """审批消息的定位与决策状态(由 approval._approve 放进 run_spec,只在本次部署的内存里流转)。"""
    ref = spec.get("_slack_card")
    return ref if isinstance(ref, dict) and ref.get("channel") and ref.get("ts") else None


def _refresh_card(spec: dict) -> None:
    ref = _card_ref(spec)
    if ref:
        update_approval(ref["channel"], ref["ts"], ref.get("ic") or "", spec, ref["decision"])


def notify_deploying(spec: dict, approver_name, when) -> None:
    ref = _card_ref(spec)
    if ref:
        ref["decision"]["deploying_at"] = when
        _refresh_card(spec)
        if _single_message():
            return
    _safe_post_result(*deploying_message(spec, approver_name, when), "部署中")


def _result_reply(spec: dict, dec: dict, ok: bool) -> str:
    """单卡模式下部署结束的线程回复:@ 申请人和审批人(同一人只 @ 一次);
    申请人映射不到 Slack 时写出 GitHub 账号并说明无法 @。"""
    ids = [u for u in (_uid(x) for x in spec.get("slack_applicant_ids") or []) if u]
    if not ids:
        gh = spec.get("operator_github")
        who = (_code(gh) if gh else esc(spec.get("operator_name") or "申请人")) + "(未映射到 Slack 用户,无法 @)"
    else:
        who = ""
    a = _uid(dec.get("user"))
    if a and a not in ids:
        ids.append(a)
    mention = " ".join(f"<@{u}>" for u in ids)
    head = " ".join(x for x in (mention, who) if x)
    body = (f"✅ 部署完成:{esc(_subject(spec))}" if ok
            else f"❌ 部署失败:{esc(_subject(spec))},详情见上方卡片。")
    return f"{head} {body}" if head else body


def notify_result(spec: dict, ok: bool, approver_name, when, log: str | None = None) -> None:
    ref = _card_ref(spec)
    if ref:
        dec = ref["decision"]
        dec.update(final="success" if ok else "failed", finished_at=when)
        if not ok and log:
            dec["log"] = log[-_LOG_TAIL:]
        _refresh_card(spec)
        if _single_message():
            # 改消息不推送通知:部署结束(成功 / 失败)都在审批消息的线程里回一条,
            # @ 申请人和审批人。不在频道里另起一条,状态仍以卡片为准。
            try:
                client.post_thread_reply(ref["channel"], ref["ts"], _result_reply(spec, dec, ok))
            except Exception as e:  # 提醒失败不影响状态
                print(f"[slack][warn] 结果提醒发送失败: {type(e).__name__}: {e}")
            return
    _safe_post_result(*result_message(spec, ok=ok, approver_name=approver_name, when=when, log=log),
                      "结果")


def notify_rejected(spec: dict, approver_name, when) -> None:
    if _single_message():
        return      # 审批消息已改成「已拒绝」并写明谁、何时
    _safe_post_result(*result_message(spec, rejected=True, approver_name=approver_name, when=when),
                      "拒绝")
