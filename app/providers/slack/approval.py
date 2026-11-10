"""Slack 审批:申请人解析 → 点击者鉴权 → 决策(批准 / 拒绝)。

与飞书的根本差异:Slack 没有"审批实例"这种平台对象。审批单就是 approvo 自己库里的一行
(instance_code = `slk-<uuid>`),「批准 / 拒绝」是人在审批消息上点按钮。所以:
  · 审批状态只有一份真相 —— store;不存在"平台说已批、我们没收到"的对账问题;
  · 但"谁有权批"也不再由平台把关,【全部落在 check_clicker 上】,它必须 fail-close。

状态流转(复用 store 的状态机与 CAS,部署复用 events._run_deploy,与飞书 / 审批直通同一条路):
    pending ──批准(try_claim_for_deploy)──▶ deploying ──▶ success | failed
    pending ──拒绝(try_transition)───────▶ rejected
批准与拒绝【都】是 CAS:两个人同一秒分别点了批准和拒绝,只有一个能生效。

鉴权规则(点击时实时校验,任何一步查询失败都按拒绝处理,状态不变):
  ① 点击发生在审批频道里的审批消息上;
  ② 点击者是真人(不是 bot / app 用户 / Slackbot),账号未停用;
  ③ 审批频道不是 Slack Connect 外部共享频道(否则别家公司的人也在成员列表里);
  ④ 点击者是审批频道的【当前】成员(conversations.members 分页拉全,不缓存);
  —— 以上对「拒绝」同样适用;拒绝是收紧,满足 ①~④ 的任何成员都可以拒(含申请人撤回自己的申请)。
  ⑤ (仅批准)该 stage 配了 approvers 白名单 → 必须在白名单里;
  ⑥ (仅批准)forbid_self_approval=true(默认)→ 不得是申请人本人,策略见 _not_self。
"""
import json
import traceback
import uuid
from datetime import datetime

from app import events, settings, store
from app.providers.slack import client, messaging

INSTANCE_PREFIX = "slk-"


def _now() -> str:
    return datetime.now(settings.CN_TZ).strftime("%Y-%m-%d %H:%M")


def new_instance_code() -> str:
    return f"{INSTANCE_PREFIX}{uuid.uuid4().hex}"


def resolve_applicant(github_actor: str | None, operator_email: str | None,
                      operator_user_id: str | None) -> dict:
    """申请人 → Slack 用户(可能多个,取并集)。返回 {ids, primary, name}。

    来源:① config 的 slack.user_map[github_actor] ② 请求里的 operator_user_id(须是 Slack ID 形状)
         ③ users.lookupByEmail(operator_email)。
    ⚖️ 取【并集】而不是"命中一个就停":这些 ID 用来判定「谁不能批自己的申请」,
       多算一个只会让自批检查更严,漏算一个才会放过自批。
    映射不出来【不拒绝受理】—— 能不能批由审批时的自批策略决定(见 _not_self),消息上会明示。
    """
    ids: list[str] = []
    name = ""
    if github_actor:
        m = settings.SLACK_USER_MAP.get(github_actor.strip().lower())
        if m:
            ids.append(m["user_id"])
            name = m.get("name") or ""
    if operator_user_id:
        uid = operator_user_id.strip()
        if settings.SLACK_USER_ID_RE.fullmatch(uid):
            ids.append(uid)
        else:
            print(f"[slack][warn] operator_user_id 不是 Slack user ID 形状,已忽略: {uid[:12]!r}")
    if operator_email:
        try:
            uid = client.user_id_by_email(operator_email)
        except Exception as e:  # 查不到只会让自批策略更严(走"未映射"分支),不阻断受理
            print(f"[slack][warn] users.lookupByEmail 失败(按未映射处理): {type(e).__name__}: {e}")
            uid = None
        if uid:
            ids.append(uid)
    uniq = list(dict.fromkeys(ids))
    return {"ids": uniq, "primary": uniq[0] if uniq else None, "name": name}


def _not_self(user_id: str, spec: dict) -> tuple[bool, str]:
    """禁止自批。

    · 申请人能映射到 Slack 用户:点击者不在申请人 ID 集合里即可。集合 = 受理时解析出的
      全部 ID ∪ 【当前】user_map 对该 GitHub 账号的映射(重启后映射可能变了,取更严的那个)。
    · 申请人映射不出来(未登记 github_actor、邮箱也查不到):我们无法直接判断点击者是不是他。
      🔴 策略(fail-close):只接受【能证明自己不是申请人】的成员 —— 即在 slack.user_map 里
      登记过、且登记的 GitHub 账号里不包含本次申请人的那些人。
      没登记的人一律不能批(他可能恰好就是那个没登记的申请人);
      请求里连 github_actor 都没有时,没有任何东西可比 ⇒ 谁都不能批,只能拒绝后补全身份重提。
    已知边界:一个人在 user_map 里登记了 A、又用没登记的 GitHub 账号 B 发起申请,本规则拦不住 ——
      所以同一个人的所有 GitHub 账号都应登记到同一个 Slack 用户下。
    """
    gh = (spec.get("operator_github") or "").strip().lower()
    applicant = set(spec.get("slack_applicant_ids") or [])
    cur = settings.SLACK_USER_MAP.get(gh) if gh else None
    if cur:
        applicant.add(cur["user_id"])
    if applicant:
        if user_id in applicant:
            return False, "禁止自批:你就是本次发版的申请人"
        return True, ""
    if not gh:
        return False, ("申请人身份未知(请求未带 github_actor,邮箱也映射不到 Slack 用户),"
                       "无法确认你不是申请人。请拒绝后让申请方补全身份重新提交")
    mine = {login for login, m in settings.SLACK_USER_MAP.items() if m["user_id"] == user_id}
    if not mine:
        return False, (f"申请人 GitHub 账号 `{gh}` 未映射到 Slack 用户,无法确认你不是申请人;"
                       f"这类申请只能由在 slack.user_map 里登记过的成员批准")
    if gh in mine:
        return False, "禁止自批:你在 slack.user_map 里登记的 GitHub 账号就是本次申请人"
    return True, ""


def check_clicker(kind: str, user_id: str, channel_id: str, spec: dict) -> tuple[bool, str, dict]:
    """点击者是否有权做这个决策。返回 (ok, 拒绝原因, users.info 结果)。规则见模块 docstring。

    🔴 每一步查询失败都按【拒绝】返回 —— 判据拿不到时,"放行"不是一个选项。
    """
    ch = settings.SLACK_APPROVAL_CHANNEL
    if not user_id:
        return False, "事件里没有点击者身份", {}
    if channel_id != ch:
        # 只认审批频道:消息被转到别处、或有人在别的频道伪造同名按钮,都不受理
        return False, "这条消息不在审批频道里,不受理", {}
    try:
        u = client.user_info(user_id)
    except Exception as e:
        return False, f"查询你的 Slack 账号失败,无法校验身份({type(e).__name__})", {}
    if u.get("is_bot") or u.get("is_app_user") or user_id == "USLACKBOT":
        return False, "机器人 / 应用账号不能审批", u
    if u.get("deleted"):
        return False, "账号已停用", u
    try:
        info = client.channel_info(ch)
    except Exception as e:
        return False, f"读取审批频道信息失败,无法校验审批人({type(e).__name__})", u
    if info.get("is_ext_shared") or info.get("is_pending_ext_shared"):
        return False, ("审批频道是 Slack Connect 外部共享频道 —— 外部组织的人也在成员列表里,"
                       "不能以它作为审批人名单。请换一个仅本组织成员的频道"), u
    try:
        members = client.channel_members(ch)
    except Exception as e:
        return False, f"读不到审批频道成员,无法校验审批人({type(e).__name__})", u
    if user_id not in members:
        return False, "你不是审批频道的当前成员", u
    if kind == "reject":
        return True, "", u
    allow = settings.slack_approvers_for(spec.get("stage"))
    if allow is not None and user_id not in allow:
        return False, f"stage「{spec.get('stage') or 'default'}」限定了审批人白名单,你不在其中", u
    if settings.SLACK_FORBID_SELF_APPROVAL:
        ok, why = _not_self(user_id, spec)
        if not ok:
            return False, why, u
    return True, "", u


def handle_action(kind: str, payload: dict, action: dict) -> None:
    """处理一次「批准 / 拒绝」点击。跑在独立后台线程里(Socket Mode 已先 ack)。

    🔴 整个函数体被 try/except 包住:后台线程里的未捕获异常只会打一段没人看的 traceback,
       表现为"点了没反应"。任何失败都要让【点的那个人】看见原因。
    """
    user_id = (payload.get("user") or {}).get("id") or ""
    container = payload.get("container") or {}
    channel = container.get("channel_id") or (payload.get("channel") or {}).get("id") or ""
    ts = container.get("message_ts") or (payload.get("message") or {}).get("ts") or ""
    ic = str(action.get("value") or "")
    response_url = payload.get("response_url") or ""

    def tell(msg: str) -> None:
        """告诉点击者结果。优先 response_url(非频道成员也收得到),失败再退回 postEphemeral。"""
        if response_url:
            try:
                client.respond_ephemeral(response_url, msg)
                return
            except Exception as e:  # 提示失败不影响任何状态
                print(f"[slack][warn] response_url 回复失败,改用 postEphemeral: {type(e).__name__}: {e}")
        try:
            client.post_ephemeral(channel or settings.SLACK_APPROVAL_CHANNEL, user_id, msg)
        except Exception as e:
            print(f"[slack][warn] ephemeral 提示发送失败: {type(e).__name__}: {e}")

    try:
        if not ic.startswith(INSTANCE_PREFIX):
            print(f"[slack][deny] 非 approvo 审批单号 value={ic[:40]!r} user={user_id}")
            tell("❌ 无法识别的审批单号,已忽略。")
            return
        rec = store.get(ic)
        if not rec:
            tell("❌ 找不到这条审批记录,本次点击无效。")
            return
        if rec["status"] != "pending":
            tell(f"ℹ️ 这条申请已处理(当前状态:{rec['status']}),本次点击无效。")
            return
        spec = json.loads(rec["spec_json"])
        ok, why, prof = check_clicker(kind, user_id, channel, spec)
        if not ok:
            print(f"[slack][deny] {kind} instance={ic} user={user_id} reason={why}")
            tell(f"❌ {why}。审批状态未改变。")
            return
        name = client.display_name(prof) or user_id
        when = _now()
        if kind == "approve":
            _approve(ic, spec, user_id, name, when, channel, ts, tell)
        else:
            _reject(ic, spec, user_id, name, when, channel, ts, tell)
    except Exception as e:  # 后台线程:不兜住就没人看得见(BLE001 已全局 ignore)
        traceback.print_exc()
        tell(f"❌ 处理出错:{type(e).__name__}。请以审批消息与结果频道的状态为准,必要时联系管理员。")


def _approve(ic: str, spec: dict, uid: str, name: str, when: str,
             channel: str, ts: str, tell) -> None:
    # 🔴 唯一的防重复部署保证:pending→deploying 原子 CAS。重复点击 / 事件重投 / 两人同时点,
    #    只有一个线程能走到下面的部署;其余的在这里得到"已被处理"。
    if not store.try_claim_for_deploy(ic):
        tell("ℹ️ 这条申请刚被别人处理了,本次点击无效。")
        return
    # 审计留痕:谁(ID + 显示名)、何时、批了哪条。日志关键字 [slack][approve] 可 grep。
    print(f"[slack][approve] instance={ic} {spec['repo']}:{spec['tag']} stage={spec.get('stage')} "
          f"approver={uid} name={name!r} at={when}")
    run_spec = dict(spec, slack_approver={"id": uid, "name": name, "at": when})
    decision = {"kind": "approved", "user": uid, "name": name, "when": when}
    messaging.update_approval(channel, ts, ic, run_spec, decision)
    # 部署:与飞书 / 审批直通【同一个】函数(发"部署中"→ 部署 → 落库 success/failed → 发结果)。
    # 这里已在独立线程里,同步调用即可;它内部兜住了部署器的一切异常。
    events._run_deploy(ic, run_spec, name)
    final = (store.get(ic) or {}).get("status")
    messaging.update_approval(channel, ts, ic, run_spec, dict(decision, final=final))


def _reject(ic: str, spec: dict, uid: str, name: str, when: str,
            channel: str, ts: str, tell) -> None:
    if not store.try_transition(ic, "pending", "rejected"):
        tell("ℹ️ 这条申请刚被别人处理了,本次点击无效。")
        return
    print(f"[slack][reject] instance={ic} {spec['repo']}:{spec['tag']} stage={spec.get('stage')} "
          f"by={uid} name={name!r} at={when}")
    run_spec = dict(spec, slack_approver={"id": uid, "name": name, "at": when})
    messaging.update_approval(channel, ts, ic, run_spec,
                              {"kind": "rejected", "user": uid, "name": name, "when": when})
    messaging.notify_rejected(run_spec, name, when)
