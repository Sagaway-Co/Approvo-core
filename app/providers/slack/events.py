"""Slack Socket Mode:事件入口 + 连接监督循环。对应飞书侧 app/main.py 的 _run_ws 与 app/events.py。

Socket Mode = approvo 主动出站建 WSS(apps.connections.open 换一次性 wss 地址),
交互事件经这条连接推过来。不需要公网回调地址、不需要验签(连接本身由 app token 认证)。

健康状态复用 app/wshealth.py —— /healthz、/readyz、/release 的 fail-close 判据
对两种审批平台是同一套,不为 Slack 另起一份。
"""
import threading
import time
import traceback

from app import settings, wshealth
from app.providers.slack import approval, client
from app.providers.slack.messaging import APPROVE_ACTION, REJECT_ACTION

_ACTIONS = {APPROVE_ACTION: "approve", REJECT_ACTION: "reject"}

# 退避参数与飞书长连接一致(见 app/main.py):凭证失效时建连会立刻失败,
# 不退避就会疯狂重连;连上并稳定运行超过 _STABLE_SEC 后再崩,退避从头开始。
_BACKOFF_START = 2
_BACKOFF_MAX = 300
_STABLE_SEC = 60
# SDK 自带断线重连(auto_reconnect_enabled)。我们每 _POLL_SEC 看一眼 is_connected():
# 断开超过 _DOWN_TOLERANCE_SEC 仍没被 SDK 救回来 → 判崩溃、拆掉客户端、走退避重建。
_POLL_SEC = 2.0
_DOWN_TOLERANCE_SEC = 30.0

# 可注入(测试用假时钟,不真 sleep)
_sleep = time.sleep
_clock = time.monotonic


def on_request(sm_client, req) -> None:
    """Socket Mode 请求入口(SDK 的 worker 线程调用)。

    🔴 先 ack、再干活:Slack 要求 3 秒内确认,否则点击者会看到报错。
       而真正的处理(查用户、拉频道成员、CAS、部署)可能要好几秒到十几分钟,
       所以 ack 之后把活丢给独立的后台线程 —— 同飞书卡片回调 _viewer_worker 的教训。
    🔴 每个 envelope 都 ack,包括我们不处理的类型:不 ack 的话 Slack 会重投 / 报错。
    """
    try:
        from slack_sdk.socket_mode.response import SocketModeResponse
        sm_client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    except Exception as e:  # ack 失败也继续处理:CAS 保证重投不会多部署(BLE001 已全局 ignore)
        print(f"[slack][warn] ack 失败 envelope={req.envelope_id}: {type(e).__name__}: {e}")
    if req.type != "interactive":
        return
    payload = req.payload or {}
    if payload.get("type") != "block_actions":
        return
    user = (payload.get("user") or {}).get("id")
    for act in payload.get("actions") or []:
        kind = _ACTIONS.get(act.get("action_id"))
        if not kind:
            continue
        print(f"[slack][event] {kind} instance={str(act.get('value'))[:48]} user={user}")
        threading.Thread(target=approval.handle_action, args=(kind, payload, act),
                         daemon=True).start()


def preflight() -> None:
    """每次(重)建连前的自检。任何一项不过都抛 → 监督循环判崩溃 → /readyz 503、/release 503。

    🔴 为什么连接之前要先查这些:Socket Mode 只用 app token 建连,bot token 失效 / 机器人
       不在审批频道时,连接照样能建成、看起来一切健康 —— 直到第一次 /release 发消息失败。
       这正是飞书那次"/healthz 绿 ≠ 审批能用"的同一类静默失效,所以把它纳入健康判据。
    """
    client.auth_test()                                    # bot token 有效
    info = client.channel_info(settings.SLACK_APPROVAL_CHANNEL)
    if not info.get("is_member"):
        raise RuntimeError("机器人不在审批频道里 —— 请先在该频道邀请(/invite)机器人")
    if info.get("is_ext_shared") or info.get("is_pending_ext_shared"):
        raise RuntimeError("审批频道是 Slack Connect 外部共享频道,不能作为审批人名单")
    if (settings.SLACK_RESULT_CHANNEL != settings.SLACK_APPROVAL_CHANNEL
            and not client.channel_info(settings.SLACK_RESULT_CHANNEL).get("is_member")):
        raise RuntimeError("机器人不在结果频道里 —— 请先在该频道邀请(/invite)机器人")


def _build_client():
    from slack_sdk.socket_mode import SocketModeClient
    cli = SocketModeClient(app_token=settings.SLACK_APP_TOKEN, web_client=client.web(),
                           auto_reconnect_enabled=True)
    cli.socket_mode_request_listeners.append(on_request)
    return cli


def _watch(cli) -> None:
    """盯住连接直到判定"救不回来"再抛。正常情况下【永不返回】。

    比飞书多一层可观测性:Socket Mode 客户端能直接回答 is_connected(),
    所以断线期间如实标成 reconnecting(不健康,fail-close),恢复后再回到 running。
    """
    down_since = None
    while True:
        _sleep(_POLL_SEC)
        if cli.is_connected():
            if down_since is not None:
                print(f"[slack][up] 连接已由 SDK 自动恢复(断开 {_clock() - down_since:.0f}s)")
                wshealth.mark_running()
                down_since = None
            continue
        now = _clock()
        if down_since is None:
            down_since = now
            wshealth.mark_reconnecting("Socket Mode 连接断开,SDK 自动重连中")
            print("[slack][warn] Socket Mode 连接断开,等待 SDK 自动重连")
        elif now - down_since >= _DOWN_TOLERANCE_SEC:
            raise RuntimeError(f"Socket Mode 断开 {now - down_since:.0f}s 未恢复(SDK 自动重连失败)")


def run_supervisor(max_cycles: int | None = None) -> None:
    """Socket Mode 监督循环:崩了就退避重建,【绝不让线程死掉】。语义与 main._run_ws 一致。

    max_cycles 仅供测试(跑 N 轮后返回);生产调用不传,永远循环。
    """
    backoff = _BACKOFF_START
    cycles = 0
    while True:
        cli = None
        ran_from: float | None = None
        try:
            wshealth.mark_starting()
            preflight()
            cli = _build_client()
            cli.connect()                       # apps.connections.open + WSS 握手;app token 无效在这里就抛
            if not cli.is_connected():
                raise RuntimeError("connect() 已返回但连接未建立")
            wshealth.mark_running()
            ran_from = _clock()
            print("[slack][up] Socket Mode 已连接")
            _watch(cli)
            raise RuntimeError("连接监视意外返回")   # 不该发生;发生了也走崩溃分支,不许静默退出
        except Exception as e:  # 宽捕获是刻意的:任何异常都不许结束本线程
            # SlackApiError 的消息自带换行;压成一行,否则 "[ws][down]" 告警行会被拆断
            wshealth.mark_crashed(" ".join(f"{type(e).__name__}: {e}".split()))
            traceback.print_exc()
        finally:
            if cli is not None:
                try:
                    cli.close()                 # 客户端构造时就起了后台线程,不 close 会随重试泄漏
                except Exception:
                    pass
        ran_for = (_clock() - ran_from) if ran_from is not None else 0.0
        if ran_for >= _STABLE_SEC:              # 曾稳定运行 → 退避归零(见 main._STABLE_SEC)
            backoff = _BACKOFF_START
        st = wshealth.snapshot()
        # 🔴 与飞书共用 "[ws][down]" 关键字:已有的日志告警规则换了审批平台照样生效。
        print(f"[ws][down] provider=slack state={st['state']} attempts={st['attempts']} "
              f"crashes={st['crashes']} ran_for={ran_for:.0f}s "
              f"err={st['last_error']} → {backoff}s 后重建")
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        _sleep(backoff)
        backoff = min(backoff * 2, _BACKOFF_MAX)
