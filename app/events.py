"""审批处理 + 启动对账。

实时性由长连接的 approval_instance 事件推送保证。事件偶发会丢(连接抖动、重启),
所以【启动时】对账一次:把进程离线期间被决策、事件没收到的 pending 实例补处理掉。

🔴 为什么不再常驻每 60s 轮询:
   原实现对【每条 pending】每 60s 调一次 get_instance,而审批等人点通过往往要
   几小时 —— 一条审批过一夜≈近千次调用。用真实数据核算过:两周的发版量就累积出
   几千次轮询调用,几乎吃光 IM 租户的基础 API 月额度,而这些调用【什么也没发现】。
   轮询本是为兜"长连接偶发丢事件",但实时性本就由推送保证,它不该按分钟计费。

事件和对账都走 process_instance,用 store.try_claim_for_deploy 原子去重,绝不重复部署。
"""
import json
import threading
import time
import traceback
from datetime import datetime

import lark_oapi as lark
from lark_oapi.api.application.v6 import P2ApplicationBotMenuV6

from app import cards, feishu, k8s, keygrant, settings, store
from app.deployers import get_deployer


def _now() -> str:
    return datetime.now(settings.CN_TZ).strftime("%Y-%m-%d %H:%M")


def _extract_decision(detail: dict):
    """从 get_instance(user_id_type=open_id) 的 timeline 取最后一次 通过/拒绝 的审批人 + 评论。"""
    approver_oid, comment = None, None
    for ev in detail.get("timeline", []) or []:
        if ev.get("type") in ("PASS", "REJECT"):
            approver_oid = ev.get("user_id") or ev.get("open_id")  # 优先 user_id(映射表按它存)
            comment = ev.get("comment")
    return approver_oid, comment


def _safe_card(card, what: str):
    """发卡片是【通知】,绝不能让它的失败阻断【部署】这个主流程。

    2026-08-09 事故：_run_deploy 第一行就是 send_card,当时飞书 API TLS 握手超时 →
    异常直接抛出 → 后面的部署【根本没执行】。而实例已被 try_claim_for_deploy 认领,
    轮询也不会再捡起来 → 审批显示"已通过",部署永远不会发生,且无任何告警。
    """
    try:
        feishu.send_card(settings.RESULT_CHAT_ID, card)
    except Exception as e:  # 通知失败只记录,不上抛(BLE001 已在 ruff.toml 全局 ignore)
        print(f"[deploy][warn] 发送{what}卡片失败(不影响部署): {type(e).__name__}: {e}")


def _run_deploy(instance_code: str, spec: dict, approver_name):
    """在独立线程跑:先弹"部署中",再部署(github 方式会等 run 跑完),最后弹结果。

    🔴 整个函数体必须被 try/except 包住:它跑在 daemon 线程里,未捕获异常只会往 stderr
    打一段 traceback,不会有人看见 —— 表现为"批了却没上线"。任何异常都必须落库成
    failed 并告警,让它可见、可重发。
    """
    _safe_card(cards.deploying_card(spec, approver_name=approver_name, when=_now()), "部署中")
    try:
        ok, log = get_deployer(spec.get("method", "kubectl")).deploy(spec)
    except Exception as e:  # 兜底:部署器任何异常都要可见(BLE001 已在 ruff.toml 全局 ignore)
        ok, log = False, f"部署异常({type(e).__name__}): {e}"
        traceback.print_exc()
    store.set_status(instance_code, "success" if ok else "failed")
    _safe_card(cards.result_card(spec, ok=ok, approver_name=approver_name, when=_now(), log=log), "结果")
    print(f"[deploy] {spec['repo']}:{spec['tag']} -> {'OK' if ok else 'FAIL'}")


def process_instance(instance_code: str):
    rec = store.get(instance_code)
    if not rec or rec["status"] != "pending":
        return  # 不是本服务的、或已处理过
    detail = feishu.get_instance(instance_code)
    status = detail.get("status")
    if status not in ("APPROVED", "REJECTED", "CANCELED", "DELETED"):
        return  # 还在审批中
    spec = json.loads(rec["spec_json"])
    approver_oid, comment = _extract_decision(detail)
    approver_name = store.name_by_id(approver_oid) if approver_oid else None

    if status == "APPROVED":
        if not store.try_claim_for_deploy(instance_code):
            return  # 别的线程/轮询已抢到
        # 部署可能耗时(github 方式要等 run),丢线程跑,别阻塞事件/轮询
        threading.Thread(target=_run_deploy, args=(instance_code, spec, approver_name),
                         daemon=True).start()
    else:
        store.set_status(instance_code, status.lower())
        feishu.send_card(settings.RESULT_CHAT_ID,
                         cards.result_card(spec, rejected=True, status=status,
                                           approver_name=approver_name, reject_comment=comment, when=_now()))
        print(f"[reject] {spec['repo']}:{spec['tag']} -> {status}")


def _handle(data: lark.CustomizedEvent) -> None:
    ev = getattr(data, "event", None) or {}
    ic = ev.get("instance_code")
    print(f"[event] instance={ic} status={ev.get('status')}")
    if ic:
        threading.Thread(target=process_instance, args=(ic,), daemon=True).start()


# ─────────────────── 卡片按钮：自助签发只读凭证 ───────────────────
# 为什么做成按钮:/viewer-token 是 API 形态,人要用得先拿共享 token + 知道自己的
# user_id + 会 curl + 手动存文件。四步下来大部分人觉得 ssh 更快 —— 通道存在 ≠ 人会用。
#
# 🔴 走【现有长连接】,不需要公网端点、不需要验签:
#    lark_oapi 的 ws 客户端已内置分发 p2.card.action.trigger(channel.py 里注册)。
#    所以这条路不引入任何新的对外攻击面。

_VIEWER_LAST: dict[str, float] = {}     # 点击者 -> 上次签发时间,防误触/连点
_VIEWER_COOLDOWN_SEC = 10


def _viewer_worker(open_id: str, user_id: str, cluster: str, namespace: str, minutes: int):
    """后台执行:校验身份 → 签发 → 私聊投递 → 群内公示。

    🔴 为什么整段丢后台:飞书要求卡片回调秒级返回,而"查群成员 + kubectl create token"
    合计可能好几秒。阻塞会被判超时并【重试】,造成重复签发。
    🔴 任何失败都必须让【点的那个人】看见:否则他点了没反应,只会转身去 ssh。
    """
    dm_id, dm_type = (open_id, "open_id") if open_id else (user_id, "user_id")

    def tell(msg: str):
        feishu.send_text(dm_id, msg, dm_type)

    try:
        # ⓪ 目标白名单【复检】。调用方已经查过一次,这里再查一次是刻意的纵深防御:
        #    本函数是被线程调用的,将来若多一个调用方(或有人改了调用顺序),
        #    入口那道校验就被绕过了。签发凭据这种事,宁可查两次。
        if not k8s.target_allowed(cluster, namespace):
            tell("❌ 目标不在允许范围内，已拒绝。")
            print(f"[viewer] worker 复检拒绝:{cluster!r}/{namespace!r}")
            return

        # ① 身份校验。🔴 卡片可以被【转发到别的群】,点击者未必是审批群成员,
        #    所以"能点到按钮"不构成身份,必须真去查群成员。
        #    ID 类型必须与 operator 实际拿到的一致,否则比对恒假、谁都签不出来。
        if user_id:
            me, kind = user_id, "user_id"
        else:
            me, kind = open_id, "open_id"
        members = feishu.get_chat_members(settings.DETAIL_CHAT_ID, kind)
        if not members:
            tell("❌ 读不到审批群成员，无法校验身份，本次未签发。请在群里反馈。")
            print(f"[viewer] 群成员读取失败,拒绝签发 {kind}={me}")
            return
        if me not in members:
            tell("❌ 你不在审批群中，拒绝签发只读凭证。")
            print(f"[viewer] 拒绝:{kind}={me} 不在审批群")
            return

        # ② 签发
        ok, out = k8s.issue_viewer_kubeconfig(cluster, namespace, minutes)
        if not ok:
            tell(f"❌ 签发失败：{out[:300]}")
            print(f"[viewer] 签发失败 {kind}={me} {cluster}/{namespace}: {out[:200]}")
            return

        # ③ 私聊投递。🔴 投递失败必须显式暴露 —— 若私聊缺权限,人点了收不到,
        #    而群里却公示"已签发",看起来像成功。这类静默失败最贵。
        sent, err = feishu.send_text(
            dm_id, cards.viewer_deliver_text(out, cluster, namespace, minutes), dm_type)

        name = (store.name_by_id(user_id) if user_id else None) or user_id or open_id
        if not sent:
            feishu.send_card(settings.RESULT_CHAT_ID, cards.result_text_card(
                f"⚠️ {name} 的只读凭证已签发，但**私聊投递失败**（{err}）。"
                f"可能是机器人缺少私聊发消息权限，请检查应用权限后重试。"))
            print(f"[viewer] 私聊投递失败 {kind}={me}: {err}")
            return

        # ④ 群内公示:可归因 + 全群可见
        feishu.send_card(settings.RESULT_CHAT_ID, cards.viewer_token_card(
            name=name, cluster=cluster, namespace=namespace, minutes=minutes,
            reason="（自助签发，未填事由）", when=_now(), via="卡片按钮"))
        print(f"[viewer] {name}({kind}={me}) {cluster}/{namespace} {minutes}m 已签发并私聊送达")
    except Exception as e:  # 后台线程未捕获异常没人看得见(BLE001 已全局 ignore)
        traceback.print_exc()
        try:
            tell(f"❌ 签发过程出错：{type(e).__name__}: {e}")
        except Exception:
            pass


# 菜单 event_key -> 动作。🔴 event_key 是【外部输入】(在 Lark 后台由人手填)，
# 同样走白名单：没登记的一律拒绝，绝不按"默认值"兜底 ——
# 缺省值指向生产是这类设计最常见的致命错误。
#
# 优先用 config 的 `menu_targets`;没配则用下面的内置示例表 —— 让"开通一个新环境"
# 从改代码变成改配置,同时不改变既有部署的行为。
MENU_TARGETS: dict[str, dict] = settings.MENU_TARGETS or {
    "viewer_prod_app": {"kind": "viewer", "cluster": "prod-cluster", "namespace": "prod-namespace",
                            "label": "示例应用 生产"},
    "viewer_qa_app":   {"kind": "viewer", "cluster": "qa", "namespace": "qa-namespace",
                            "label": "示例应用 QA"},
    # 受限环境 在同一个 ACK 集群的 受限 namespace。approvo 现有凭据对 受限 namespace 【全部 403】(实测)，
    # 要签发得在 受限 namespace 建 viewer SA —— 那是动 受限环境 生产，必须 该环境的负责团队点头。
    # 在那之前明确回复"未开通"，而不是抛一个看不懂的 403。
    "viewer_prod_restricted":     {"kind": "not_ready", "label": "受限环境 生产"},
    "viewer_qa_restricted":       {"kind": "not_ready", "label": "受限环境 QA"},
    "release_token":       {"kind": "release_key", "label": "发版密钥"},
}

_MENU_LAST: dict[str, float] = {}      # 点击者 -> 上次触发时间，防误触/连点
_MENU_COOLDOWN_SEC = 5


def _dm(open_id: str, user_id: str, text: str) -> None:
    """私聊回复点击者。open_id 恒有，user_id 需应用权限，故优先 open_id。"""
    if open_id:
        feishu.send_text(open_id, text, "open_id")
    elif user_id:
        feishu.send_text(user_id, text, "user_id")


def _menu_worker(open_id: str, user_id: str, name: str, key: str) -> None:
    """后台执行菜单动作。

    🔴 为什么丢后台：事件回调应尽快返回，而"查群成员 + kubectl create token"
    可能好几秒。阻塞会被判超时并重投，造成重复签发。
    🔴 任何失败都要让【点的那个人】看见，否则他只会转身去 ssh。
    """
    try:
        target = MENU_TARGETS.get(key)
        if not target:
            _dm(open_id, user_id, f"❌ 未知的菜单项（{key}），已拒绝。请联系管理员核对配置。")
            print(f"[bot-menu] 拒绝:未登记的 event_key={key!r} clicker={open_id or user_id}")
            return

        # 身份校验：必须确实在审批群里。ID 类型要与拿到的一致，否则比对恒假。
        if user_id:
            me, kind = user_id, "user_id"
        else:
            me, kind = open_id, "open_id"
        members = feishu.get_chat_members(settings.DETAIL_CHAT_ID, kind)
        if not members:
            _dm(open_id, user_id, "❌ 读不到审批群成员，无法校验身份，本次未签发。")
            return
        if me not in members:
            _dm(open_id, user_id, "❌ 你不在审批群中，拒绝签发。")
            print(f"[bot-menu] 拒绝:{kind}={me} 不在审批群 key={key}")
            return

        # 真名解析多级兜底：事件里的 operator_name 常为空（缺通讯录权限），
        # 而"谁取的"必须看得懂 —— 退化成一串 ID 就等于没有归因。
        who = (name
               or (store.name_by_id(user_id) if user_id else None)
               or feishu.user_name_by_id(open_id or user_id,
                                         "open_id" if open_id else "user_id")
               or me)
        if target["kind"] == "not_ready":
            _dm(open_id, user_id,
                f"⚠️ {target['label']} 凭证尚未开通。\n"
                f"该环境在 受限 namespace，approvo 当前对它无任何权限（这是刻意的隔离）。\n"
                f"需 该环境的负责团队授权并建好只读 SA 后才能开通。")
            print(f"[bot-menu] {who} 请求了未开通的 {target['label']}")
            return

        if target["kind"] == "release_key":
            grant, ttl = keygrant.issue(me, who)
            _dm(open_id, user_id,
                f"🔑 取发版密钥（RELEASE_TOKEN）的一次性凭证\n"
                f"\n有效期 {ttl} 分钟，且【只能用一次】。用它换取密钥：\n"
                f"  curl -sS -X POST {settings.PUBLIC_GATE_URL}/release-key \\\n"
                f"    -H 'Content-Type: application/json' \\\n"
                f"    -d '{{\"grant\":\"{grant}\"}}'\n"
                f"\n⚠️ 这里发的是【临时凭证】不是密钥本身 —— 聊天记录永久留存，"
                f"而它 {ttl} 分钟后就失效了。\n"
                f"⚠️ 取到密钥后请勿粘贴进任何聊天/文档；配置 GitHub secret 时用管道传值。")
            feishu.send_card(settings.RESULT_CHAT_ID, cards.result_text_card(
                f"🔑 **{who}** 申请了发版密钥取用凭证（{ttl} 分钟一次性）。\n"
                f"如非本人操作请立即在群内反馈。", title="🔑 发版密钥取用"))
            print(f"[bot-menu] {who}({kind}={me}) 申请 release_token grant，{ttl}m")
            return

        # viewer：复用既有签发逻辑
        cluster, ns = target["cluster"], target["namespace"]
        if not k8s.target_allowed(cluster, ns):
            _dm(open_id, user_id, f"❌ 目标 {cluster}/{ns} 不在白名单内，已拒绝。")
            print(f"[bot-menu] 拒绝:目标不在白名单 {cluster}/{ns}")
            return
        _viewer_worker(open_id, user_id, cluster, ns, 30)
    except Exception as e:  # 后台线程未捕获异常没人看得见
        traceback.print_exc()
        try:
            _dm(open_id, user_id, f"❌ 处理出错：{type(e).__name__}: {e}")
        except Exception:
            pass


def _on_bot_menu(data: P2ApplicationBotMenuV6) -> None:
    """Lark 机器人菜单点击事件。

    ⚖️ 为什么用菜单而不是卡片按钮：卡片按钮需要在开放平台单独开通"卡片回调"，
    且卡片得先有人发出来才能点；菜单常驻在机器人会话里，随时可用。
    2026-08-10 owner 实测卡片按钮"不知道怎么触发"后决定改用菜单。
    """
    ev = getattr(data, "event", None)
    key = (getattr(ev, "event_key", "") or "") if ev else ""
    op = getattr(ev, "operator", None) if ev else None
    name = (getattr(op, "operator_name", "") or "") if op else ""
    oid = getattr(op, "operator_id", None) if op else None
    open_id = (getattr(oid, "open_id", "") or "") if oid else ""
    user_id = (getattr(oid, "user_id", "") or "") if oid else ""

    # 🔴 原样打出收到的 event_key：它由人在 Lark 后台手填，不能靠猜。
    # 日志里只留 ID 前 8 位，够定位又不泄露完整身份。
    print(f"[bot-menu] key={key!r} name={name!r} "
          f"open_id={open_id[:8]}… user_id={user_id[:8]}…")

    if not (open_id or user_id):
        print("[bot-menu] 事件里没有点击者身份，拒绝")
        return

    who = open_id or user_id
    now = time.time()
    if now - _MENU_LAST.get(who, 0) < _MENU_COOLDOWN_SEC:
        print(f"[bot-menu] 冷却期内忽略 key={key}")
        return
    _MENU_LAST[who] = now

    threading.Thread(target=_menu_worker,
                     args=(open_id, user_id, name, key), daemon=True).start()


def build_handler():
    return (lark.EventDispatcherHandler.builder("", "")
            .register_p1_customized_event("approval_instance", _handle)
            .register_p2_application_bot_menu_v6(_on_bot_menu)
            .build())


def reconcile_pending_once():
    """启动时对账一次:把进程离线/重启期间被决策、长连接没收到的 pending 实例补处理掉。

    🔴 只跑一次,不再常驻轮询(原因见模块 docstring:常驻轮询会吃光 IM 的 API 月额度)。
    调用成本 = 启动瞬间的 pending 条数,通常为 0;每次重启才发生一次。

    ⚠️ 残留风险:若在【运行中】长连接恰好抖动的那几秒里某条审批被决策,该事件可能丢,
    这条 release 会一直 pending 且不部署。当前靠"下次重启时的这次对账"补上。若要覆盖
    运行中丢事件,应在长连接【重连】回调里再调一次本函数(而不是退回按分钟轮询)。
    """
    try:
        pend = store.list_pending()
        print(f"[reconcile] 启动对账:{len(pend)} 条 pending 待核对")
        for ic in pend:
            process_instance(ic)
        print("[reconcile] 启动对账完成")
    except Exception as e:
        print(f"[reconcile] error: {e}")
