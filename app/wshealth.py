"""长连接(WS)健康状态 —— 让「审批链路是否真的可用」成为可观测、可判据的东西。

🔴 为什么需要它:
用一个无效的 app_id 启动 → `lark.ws.Client.start()` 在【连接阶段】直接 raise
→ 未捕获异常把整个 WS 线程带走,且【没有任何重试】。
而 uvicorn 在主线程毫发无伤。实测:

    connect failed 次数: 1
    线程崩溃次数:       1     ← 抛异常后线程死了
    容器 restarts:      0
    容器 ready:         true
    /healthz:           {"ok":true}

于是形成一个【静默失效】:
  · 卡片还能正常创建(走 HTTP REST,与 WS 完全独立)
  · 人还能在 IM 里正常点通过
  · 但审批事件永远收不到 → 什么都不会部署 → 且没有任何告警
  · CI 侧的 fail-close 只看 POST /release 是否 2xx —— 而那时 HTTP 是好的

⚠️ 真实风险不在"凭证配错"(那个一眼看得出),而在【运行中断开】:
凭证轮换、应用被停用、或一次网络抖动让 SDK 内部重连耗尽。
之后一切"看起来"正常,直到有人发现某个版本压根没上。

`main._run_ws` 原实现是裸的 `cli.start()`。注释写着「阻塞 + 自动断线重连」——
那句话本身是真的,但重连是 SDK 在 start() 【内部】的行为,前提是 start() 已经
成功进入阻塞。连接阶段就 raise、或 SDK 内部重连最终放弃并上抛,异常都会穿透
到线程顶层,threading 打个 traceback 就结束了。

⚖️ 判据怎么选(这是本模块唯一有难度的地方):
`cli.start()` 是阻塞调用,SDK 不提供"已连上"回调,所以我们能拿到的最强信号是
「start() 正在阻塞中且未抛异常」。它【不等于】链路一定通,但足以区分我们真正
关心的两种状态:
  · 从来没连上 / 刚崩溃           → start() 很快 raise,running 维持不住
  · SDK 认为自己在工作(含内部重连) → 持续阻塞在 start() 里

故 healthy() 额外要求「running 已持续 GRACE_SEC 秒」—— 无效凭证会在几百毫秒内
raise,撑不过这个门槛。这是【刻意的保守】:宁可把"刚重启的几秒"判为不健康
(fail-close 拒几次发版,CI 重试即可),也不要把"连不上"判成健康。

🔴 为什么放内存不落库:它描述的是【本进程当前】的连接状态,重启即无意义。
落库反而会让"上一个进程的状态"污染新进程的判断。
"""
import threading
import time

# running 需要维持这么久才算健康。无效凭证在几百毫秒内就会 raise,
# 取 15s 足以过滤掉它们,又不会让正常重启后的不可用窗口过长。
GRACE_SEC = 15.0

_lock = threading.Lock()
_st: dict = {
    "state": "init",        # init / starting / running / crashed
    "since": 0.0,           # 进入当前 state 的时刻
    "attempts": 0,          # 累计尝试建连次数
    "crashes": 0,           # 累计崩溃次数
    "last_error": "",       # 最近一次错误(类型: 消息)
    "last_error_at": 0.0,
}


def mark_starting() -> None:
    with _lock:
        _st["state"] = "starting"
        _st["since"] = time.time()
        _st["attempts"] += 1


def mark_running() -> None:
    """即将进入 start() 阻塞。配合 GRACE_SEC 过滤"刚 running 就崩"的情况。"""
    with _lock:
        _st["state"] = "running"
        _st["since"] = time.time()


def mark_crashed(err: str) -> None:
    with _lock:
        _st["state"] = "crashed"
        _st["since"] = time.time()
        _st["crashes"] += 1
        _st["last_error"] = err[:300]        # 截断:错误文本可能很长
        _st["last_error_at"] = time.time()


def healthy() -> bool:
    """审批链路是否可用。🔴 fail-close:未知一律判不健康。"""
    with _lock:
        return _st["state"] == "running" and (time.time() - _st["since"]) >= GRACE_SEC


def snapshot() -> dict:
    """给 /healthz 与 /readyz 用。不含任何凭据。"""
    with _lock:
        s = dict(_st)
    now = time.time()
    return {
        "state": s["state"],
        "healthy": s["state"] == "running" and (now - s["since"]) >= GRACE_SEC,
        "in_state_sec": round(now - s["since"], 1) if s["since"] else None,
        "attempts": s["attempts"],
        "crashes": s["crashes"],
        "last_error": s["last_error"] or None,
        "last_error_age_sec": round(now - s["last_error_at"], 1) if s["last_error_at"] else None,
    }
