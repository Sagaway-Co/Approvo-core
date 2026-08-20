"""飞书交互卡片:
- submit_card:详细申请卡 → 审批群(应用/环境/部署目标/版本/申请人/申请时间/镜像)
- result_card:结果卡 → 结果群(版本已部署;应用/环境/目标/版本/@申请人/镜像/审批人/审批信息/时间;成功卡附本次变更;不含日志)
"""

ENV_CN = {"prod": "生产", "production": "生产", "staging": "测试", "test": "测试", "dev": "开发"}


def _env_cn(env: str | None) -> str:
    return ENV_CN.get((env or "").lower(), env or "-")


def _applicant_lines(spec: dict) -> str:
    """申请人字段两行显示 (submit_card 用):
      - 已映射真名: '<真名>\\n`<github_actor>`'    (第二行小字, 让审批人核对)
      - 仅 GitHub actor 未映射: '`<github_actor>`\\n(未映射)'
      - 仅邮箱: '<email>'
      - 兜底: '-'
    """
    gh = spec.get("operator_github") or ""
    name = spec.get("operator_name") or ""
    if spec.get("operator_mapped") and name and gh:
        return f"{name}\n`{gh}`"
    if gh:
        return f"`{gh}`\n(未映射)"
    return name or "-"


def _applicant(spec: dict) -> str:
    """有 id(user_id/open_id)就 @(卡片会自动显示真名),否则退化为纯名字。"""
    uid = spec.get("operator_id")
    if uid:
        return f'<at id="{uid}"></at>'
    return spec.get("operator_name") or "-"


def _fields(pairs):
    return [{"is_short": True, "text": {"tag": "lark_md", "content": f"**{k}**\n{v}"}} for k, v in pairs]


def _md_safe(s: str) -> str:
    """PR 标题/提交信息进 lark_md,别让 []` 破坏链接和行内代码。"""
    return (s or "").replace("[", "【").replace("]", "】").replace("`", "'")


def _title_prefix(spec: dict) -> str:
    """项目名前缀,让卡片标题一眼分清项目 (比如"MyProject · 发版申请 · 待审批")."""
    proj = spec.get("project")
    return f"{proj} · " if proj else ""


def _is_prod(spec: dict) -> bool:
    """判断是否生产/紧急发版,决定卡片是否加 ⚠️ 装饰围栏 + 红色 header."""
    stage = (spec.get("stage") or "").lower()
    env = (spec.get("env") or "").lower()
    return stage in ("release", "hotfix") or env in ("prod", "production")


def _prod_warning() -> dict:
    """⚠️ 装饰行(生产卡片顶部+底部各一行,形成围栏效果强化警示)."""
    return {"tag": "div", "text": {"tag": "lark_md", "content": "⚠️" * 24}}


def _changes_block(spec: dict) -> list:
    """「本次变更」区块:N 个 PR + 未关联 PR 的直接提交。让审批人看出有没有带上别人的代码。"""
    ch = spec.get("changes")
    if not ch:
        return []

    def div(txt):
        return {"tag": "div", "text": {"tag": "lark_md", "content": txt}}

    st = ch.get("status")
    if st == "first":
        return [div("**📦 本次变更**\n首次经网关记录发版,无上一版本可对比")]
    if st == "error":
        # 把 exception 详情显示出来(通常是 GitHub App 权限 / compare 404 / 网络),
        # 审批人一眼看清是不是要 block(比如 App 未装到源码仓时全组织卡都会这样).
        err = _md_safe(ch.get("error") or "未知错误")
        base = ch.get("base") or "?"
        head = (ch.get("head") or "?")[:12]
        return [div(f"**📦 本次变更**\n⚠️ 变更清单获取失败,请人工核对(不影响审批)\n· 对比区间: `{base}...{head}`\n· 错误: `{err}`")]
    if st == "nodiff":
        return [div("**📦 本次变更**\n与已部署版本相比无新增提交(重发或回退)")]

    prs, direct = ch.get("prs") or [], ch.get("direct_commits") or []
    lines = [f"**📦 本次变更:{len(prs)} 个 PR · 共 {ch.get('total_commits', 0)} 个提交**"]
    for p in prs[:10]:
        lines.append(f"· [#{p['number']}]({p['url']}) {_md_safe(p['title'])} — {_md_safe(p.get('author') or '?')}")
    if len(prs) > 10:
        lines.append(f"· …共 {len(prs)} 个 PR,其余见完整对比")
    if direct:
        lines.append(f"**⚠️ 未关联 PR 的直接提交:{len(direct)} 个**")
        for d in direct[:5]:
            lines.append(f"· `{d['sha']}` {_md_safe(d['message'])} — {_md_safe(d.get('author') or '?')}")
        if len(direct) > 5:
            lines.append(f"· …等 {len(direct)} 个")
    if ch.get("compare_url"):
        lines.append(f"[查看完整代码对比]({ch['compare_url']})")
    return [div("\n".join(lines))]


def _image_block(spec: dict) -> dict:
    """「镜像」栏。清单应用类申请没有 image_repo，server 会把 image 填成 tag 本身
    (一串 commit sha)，显示出来对审批人毫无意义还容易误导成"要发这个镜像"。
    此时明说本次不改镜像。"""
    img = spec.get("image") or ""
    if img == spec.get("tag"):
        return {"tag": "div", "text": {"tag": "lark_md",
                "content": "**镜像**\n本次不改镜像（清单应用，镜像沿用集群现值）"}}
    return {"tag": "div", "text": {"tag": "lark_md", "content": f"**镜像**\n`{img}`"}}


def _env_change_block(spec: dict) -> list:
    """「环境变更」区块 —— 每张卡固定有这一行，回答审批人最该问的问题：
    **这次除了换镜像，还动了配置吗？**

    固定显示而非"有才显示"：大多数发版是【新镜像 + 配套环境变量变更】一起的。
    若只在有变更时才出现，审批人无法区分「没有变更」和「有变更但没申报」——
    前者安全，后者是事故。

    ⚠️ 措辞保守：approvo 看不到集群实际状态，只知道【本次申请附没附带变更内容】。
    写死"仅镜像变更"等于替它断言没验证过的事，故说"本次申请未附带"。

    🔴 渲染：飞书 lark_md 【不支持 ``` 围栏代码块】——写了会把反引号字面显示出来
    (2026-08-09 实测踩到)。改为逐行渲染，diff 的 +/- 行用行内 code 保持对齐可读。
    """
    notes = (spec.get("notes") or "").strip()
    if not notes:
        return [{"tag": "div", "text": {"tag": "lark_md",
                 "content": "**🔧 环境变更**\n仅镜像变更 —— 本次申请未附带环境变量/配置变更"}}]
    MAX_LINES, MAX_CHARS = 40, 2000
    lines = notes.splitlines()
    truncated = len(lines) > MAX_LINES
    out = []
    for ln in lines[:MAX_LINES]:
        t = ln.rstrip()
        if not t:
            out.append("")
        elif t[0] in "+-" or t.startswith(("MOD", "NEW", "SAME")):
            out.append("`" + _md_safe(t)[:160] + "`")   # 变更行用行内 code，避免被 md 吃掉
        else:
            out.append(_md_safe(t)[:160])
    body = "\n".join(out)[:MAX_CHARS]
    if truncated:
        body += f"\n… 共 {len(lines)} 行，完整内容见流水线日志"
    return [{"tag": "div", "text": {"tag": "lark_md", "content": "**🔧 环境变更**\n" + body}}]


def _db_change_block(spec: dict) -> list:
    """「数据库变更」区块 —— 只要这条 release 配了迁移目录,就【固定显示】。

    🔴 为什么必须有:在此之前,一次带着迁移的发版,卡片上只写
       "仅镜像变更 —— 未附带环境变量/配置变更"。审批人以为自己批的是换镜像,
       实际发生的是改生产库结构 —— 因为迁移文件在部署仓,
       业务仓的变更清单天然看不见它。

    ⚖️ 三种状态严格区分,不许混:
       ok    → 列出文件,破坏性语句加红标
       none  → 该时间基线之后确实没有迁移变更
       unknown → 【查不到】。绝不显示成"没有变更" —— 那是把无知包装成安全。

    ⚠️ 没有 `db_changes` 字段(= 这条 release 没配 `db_migrations`)时返回空列表:
       没开这个功能的应用不该被塞一行"无法判定"的噪音。
       但只要开了、哪怕查询失败,就必须如实显示"无法判定"。
    """
    d = spec.get("db_changes")
    if d is None:
        return []
    st = (d or {}).get("status")
    if st == "ok" and d.get("files"):
        rows = []
        for f in d["files"][:15]:
            mark = {"added": "新增", "modified": "修改", "removed": "删除"}.get(f.get("status"), f.get("status") or "")
            danger = ("  🔴 " + " / ".join(f["danger"])) if f.get("danger") else ""
            rows.append(f"`{_md_safe(f['name'])[:80]}` {mark}{danger}")
        more = f"\n… 共 {len(d['files'])} 个文件" if len(d["files"]) > 15 else ""
        note = ("\n⚠️ 口径:自本应用上次成功部署以来的变更(时间口径);"
                "实际会执行哪些以迁移门禁为准。")
        warn = ("\n🔴 含破坏性语句 —— 镜像可回滚、schema 不可回滚,"
                "回滚镜像只会让旧代码撞上已变的表结构。"
                if any(f.get("danger") for f in d["files"]) else "")
        return [{"tag": "div", "text": {"tag": "lark_md",
                 "content": "**🗄 数据库变更**\n" + "\n".join(rows) + more + note + warn}}]
    if st == "none":
        return [{"tag": "div", "text": {"tag": "lark_md",
                 "content": "**🗄 数据库变更**\n无 —— 自上次成功部署以来迁移目录没有变更"}}]
    reason = (d or {}).get("reason") or "未查询"
    return [{"tag": "div", "text": {"tag": "lark_md",
             "content": f"**🗄 数据库变更**\n⚠️ 无法判定（{_md_safe(str(reason))[:100]}）"
                        f" —— 请在批准前自行确认本次是否含迁移"}}]


def submit_card(spec: dict) -> dict:
    """详细申请卡 → 审批群。生产/紧急发版会加 ⚠️ 围栏 + 红色 header + 强化提示."""
    is_prod = _is_prod(spec)
    header_tmpl = "red" if is_prod else "blue"
    prefix = _title_prefix(spec)
    title = f"🚀 {prefix}生产发版申请 · 待审批" if is_prod else f"🚀 {prefix}发版申请 · 待审批"
    note_content = (
        "⚠️ 生产发版,审批人 = 指定负责人(见审批定义)。请在飞书「审批」中谨慎处理"
        if is_prod
        else "审批人=本群成员(或签,任一通过即部署)。请在飞书「审批」中处理"
    )
    body = [
        {"tag": "div", "fields": _fields([
            ("应用", spec["repo"]),
            ("环境", _env_cn(spec.get("env"))),
            ("部署目标", spec.get("platform", "-")),
            ("版本", spec["tag"]),
            ("申请人", _applicant_lines(spec)),
            ("申请时间", spec.get("submit_time", "-")),
        ])},
        _image_block(spec),
        *_env_change_block(spec),
        *_db_change_block(spec),
        *_changes_block(spec),
        {"tag": "note", "elements": [{"tag": "plain_text", "content": note_content}]},
    ]
    if is_prod:
        body = [_prod_warning()] + body + [_prod_warning()]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": header_tmpl, "title": {"tag": "plain_text", "content": title}},
        "elements": body,
    }


def deploying_card(spec: dict, approver_name: str | None = None, when: str | None = None) -> dict:
    """已审批·部署中 → 结果群(审批通过后立刻弹,部署完再弹 result_card)。"""
    is_prod = _is_prod(spec)
    prefix = _title_prefix(spec)
    title = f"🚀 {prefix}生产部署中…" if is_prod else f"🚀 {prefix}已审批 · 部署中…"
    body = [
        {"tag": "div", "fields": _fields([
            ("应用", spec["repo"]),
            ("环境", _env_cn(spec.get("env"))),
            ("部署目标", spec.get("platform", "-")),
            ("版本", spec["tag"]),
            ("申请人", _applicant(spec)),
            ("审批人", approver_name or "-"),
            ("审批信息", "审批通过,部署中"),
            ("时间", when or "-"),
        ])},
        _image_block(spec),
        *_env_change_block(spec),
        *_db_change_block(spec),
    ]
    if is_prod:
        body = [_prod_warning()] + body + [_prod_warning()]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": title}},
        "elements": body,
    }


def result_card(spec: dict, ok: bool = True, rejected: bool = False, status: str | None = None,
                approver_name: str | None = None, reject_comment: str | None = None,
                when: str | None = None, log: str | None = None) -> dict:
    """结果卡 → 结果群。成功不含日志;失败时附「失败详情」(拉不到镜像/健康检查不过等)。"""
    is_prod = _is_prod(spec)
    prod_prefix = "生产" if is_prod else ""
    prefix = _title_prefix(spec)
    if rejected:
        header = {"template": "grey", "title": {"tag": "plain_text", "content": f"🚫 {prefix}{prod_prefix}发版被拒绝"}}
        approval_info = "已拒绝" + (f":{reject_comment}" if reject_comment else "")
    elif ok:
        header = {"template": "green", "title": {"tag": "plain_text", "content": f"✅ {prefix}{prod_prefix}版本已部署"}}
        approval_info = "审批通过"
    else:
        header = {"template": "red", "title": {"tag": "plain_text", "content": f"❌ {prefix}{prod_prefix}部署失败"}}
        approval_info = "审批通过(部署失败)"

    elements = [
        {"tag": "div", "fields": _fields([
            ("应用", spec["repo"]),
            ("环境", _env_cn(spec.get("env"))),
            ("部署目标", spec.get("platform", "-")),
            ("版本", spec["tag"]),
            ("申请人", _applicant(spec)),
            ("审批人", approver_name or "-"),
            ("审批信息", approval_info),
            ("时间", when or "-"),
        ])},
        _image_block(spec),
        *_env_change_block(spec),
        *_db_change_block(spec),
    ]
    # 仅「版本已部署」同步带变更清单(部署中/失败/拒绝卡不带,不另发卡片)
    if ok and not rejected:
        elements.extend(_changes_block(spec))
    # 仅失败时附详情(成功保持干净)
    if not rejected and not ok and log:
        tail = log[-1800:]
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**失败详情**\n```\n{tail}\n```"}})

    if is_prod:
        elements = [_prod_warning()] + elements + [_prod_warning()]
    return {"config": {"wide_screen_mode": True}, "header": header, "elements": elements}


def result_text_card(text: str, title: str = "⚠️ 需要注意") -> dict:
    """一段话的通知卡。用于"发生了需要人知道的事"但又不值得专门做一种卡的场合。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}],
    }


def viewer_panel_card(targets: list[dict]) -> dict:
    """审批群常驻面板：点按钮即签发只读凭证,机器人私聊发回。

    🔴 为什么要有它(通道存在 ≠ 人会用):
    /viewer-token 是 API 形态,人要用得先拿共享 token + 知道自己的 user_id + 会 curl。
    四步下来大部分人觉得 ssh 更快,通道就白建了。按钮把这四步压成一步。

    🔴 顺带解决了一个安全问题,而不只是体验:
    API 形态的身份是【调用方自报 user_id】+ 共享 RELEASE_TOKEN,而 token 人人可得,
    等于谁都能以别人的名义签发。卡片回调的 operator 由飞书平台盖章、不可伪造,
    且按钮只在审批群里可见 —— 身份强度反而比 API 那条路更高。
    """
    buttons = []
    for t in targets:
        is_prod = t.get("prod")
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": t["label"]},
            "type": "danger" if is_prod else "default",
            "value": {"action": "viewer_token", "cluster": t["cluster"],
                      "namespace": t["namespace"], "minutes": t.get("minutes", 30)},
        })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text", "content": "🔍 只读诊断凭证 · 自助签发"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
             "点下面的按钮，机器人会**私聊**把 kubeconfig 发给你，用来查日志 / 看 pod 状态。\n"
             "**不必再 ssh 进服务器。**"}},
            {"tag": "action", "actions": buttons},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
             "只读：可看 pod / 日志 / 状态；不含 secrets、不能 exec、不能改任何东西。"
             "默认 30 分钟后自动失效。每次签发都会在本群公示，谁签的所有人可见。"}]},
        ],
    }


def viewer_deliver_text(kubeconfig: str, cluster: str, namespace: str, minutes: int) -> str:
    """私聊投递的正文。纯文本 —— 卡片的 lark_md 会破坏 kubeconfig 的原样内容。

    附上可直接粘的用法,否则人拿到一段 YAML 仍然不知道下一步做什么,
    还是会退回去 ssh。
    """
    return (
        f"🔍 只读 kubeconfig 已签发（{cluster} / {namespace}，{minutes} 分钟后失效）\n"
        f"\n用法：把下面整段存成文件后\n"
        f"  export KUBECONFIG=~/viewer.yaml\n"
        f"  kubectl -n {namespace} get pods\n"
        f"  kubectl -n {namespace} logs -f <pod名>\n"
        f"\n只读：不能 exec、不能改、看不到 secrets。过期后重新点按钮即可。\n"
        f"⚠️ 请勿转发他人 —— 这段内容等同于你的身份。\n"
        f"\n----- 从下一行开始整段复制 -----\n{kubeconfig}"
    )


def viewer_token_card(name: str, cluster: str, namespace: str, minutes: int,
                      reason: str, when: str, via: str | None = None) -> dict:
    """只读凭证签发通知 —— 全群可见即是威慑。

    刻意做成【事后通知】而非【事前审批】：只读且短期，若要求审批，
    "查个日志等半小时"会把人逼回去 ssh，通道就白建了。
    但必须可归因、必须看得见。

    via 标注来源(卡片按钮 / API)：两条路的【身份强度不同】——按钮的点击者由飞书盖章、
    不可伪造；API 是自报 user_id + 共享 token。出事时这个差别决定了追查方向,
    所以必须在卡上区分,不能混成一样。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "grey",
                   "title": {"tag": "plain_text", "content": "🔍 只读凭证已签发"}},
        "elements": [
            {"tag": "div", "fields": _fields([
                ("申请人", _md_safe(name)),
                ("有效期", f"{minutes} 分钟"),
                ("目标", f"{_md_safe(cluster)} / {_md_safe(namespace)}"),
                ("时间", when),
            ] + ([("签发方式", _md_safe(via))] if via else []))},
            {"tag": "div", "text": {"tag": "lark_md",
             "content": "**事由**\n" + (_md_safe(reason) if reason else "(未填写)")}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
             "只读：可看 pod/日志/状态，不含 secrets、不能 exec、不能改任何东西。"
             "如非本人操作请立即在群内反馈。"}]},
        ],
    }


def secret_done_card(ns: str, name: str, key: str, fingerprint: str, when: str) -> dict:
    """Secret 变更完成通知。只报【指纹】不报值 —— 事后能核对"改成了哪一版"，但泄露不了内容。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green",
                   "title": {"tag": "plain_text", "content": "🔐 Secret 已更新"}},
        "elements": [
            {"tag": "div", "fields": _fields([
                ("目标", f"{_md_safe(ns)}/{_md_safe(name)}"),
                ("key", _md_safe(key)),
                ("值指纹", f"`sha256:{fingerprint}`"),
                ("时间", when),
            ])},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
             "值不落库、不入 git、不进日志；此处只记指纹以便事后核对版本。"
             "如引用该 Secret 的服务需重启才生效，请另走配置变更通道。"}]},
        ],
    }
