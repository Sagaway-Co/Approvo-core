"""入口:启动长连接事件监听 + HTTP 服务。

长连接客户端 .start() 是阻塞的,丢后台线程跑;HTTP 用 uvicorn 在主线程跑。
"""
import threading
import time
import traceback

import lark_oapi as lark
import uvicorn

from app import feishu, settings, wshealth
from app.events import build_handler, reconcile_pending_once

# 重启退避:凭证失效时 start() 会立刻 raise,不退避就会疯狂重连并把 IM 的 API 额度打爆
# (而额度耗尽本身又会让 IM 全线不可用)。
_BACKOFF_START = 2
_BACKOFF_MAX = 300
# 连上并稳定运行超过这个时长,视为"曾经健康",崩溃后退避【从头开始】。
# 🔴 没有这一条,退避会单调累积:一条连了半天才偶发断开的链路,会被迫等到上次
#    攒到的 300s 才重连 —— 而它本该立刻恢复。退避是为了压住"连不上时的狂重试",
#    不该惩罚"运行良好但偶发抖动"。
_STABLE_SEC = 60


def _run_ws():
    """WS 监督循环:崩了就退避重启,【绝不让线程死掉】。

    🔴 原实现是裸的 `cli.start()`:连接阶段 raise → 未捕获异常把线程带走 → 静默失效
    (容器 ready、/healthz 绿、卡片能发、人能点通过,但事件永远收不到 → 什么都不部署)。
    完整分析见 app/wshealth.py 模块注释。

    注意 `cli.start()` 【正常情况下永不返回】。它返回了本身就是异常信号
    (SDK 放弃重连),所以返回后也要走 crashed 分支 —— 否则会静默退出循环,
    等于把刚修掉的 bug 换个形式再犯一次。
    """
    backoff = _BACKOFF_START
    while True:
        ran_from = 0.0
        try:
            wshealth.mark_starting()
            cli = lark.ws.Client(
                settings.APP_ID, settings.APP_SECRET,
                domain=settings.LARK_DOMAIN,   # feishu 或 lark endpoint(见 settings.py)
                event_handler=build_handler(),
                log_level=lark.LogLevel.INFO,
            )
            wshealth.mark_running()
            ran_from = time.time()
            cli.start()                        # 阻塞;SDK 内部自动断线重连
            wshealth.mark_crashed("client.start() 意外返回(SDK 已放弃重连)")
            print("[ws][warn] start() 意外返回 —— SDK 放弃重连,将重建客户端")
        except Exception as e:  # 宽捕获是刻意的:任何异常都不许结束本线程
            wshealth.mark_crashed(f"{type(e).__name__}: {e}")
            traceback.print_exc()
        ran_for = (time.time() - ran_from) if ran_from else 0.0
        if ran_for >= _STABLE_SEC:             # 曾稳定运行 → 退避归零(见 _STABLE_SEC)
            backoff = _BACKOFF_START
        st = wshealth.snapshot()
        # 🔴 这行日志是唯一的运行期线索(卡片告警在 IM 本身不可用时也发不出去),
        #    务必保持可 grep:日志告警按 "[ws][down]" 配规则。
        print(f"[ws][down] state={st['state']} attempts={st['attempts']} "
              f"crashes={st['crashes']} ran_for={ran_for:.0f}s "
              f"err={st['last_error']} → {backoff}s 后重建")
        time.sleep(backoff)
        backoff = min(backoff * 2, _BACKOFF_MAX)


def main():
    # 启动即订阅审批事件(幂等)。不订阅就收不到 approval_instance。
    try:
        feishu.subscribe(settings.APPROVAL_CODE)
    except Exception as e:  # 订阅失败不阻断启动
        print(f"[main] subscribe error: {e}")

    threading.Thread(target=_run_ws, daemon=True).start()
    # 启动时对账一次:补上进程离线/重启期间被决策、长连接没收到的审批。
    # 稳态实时性由长连接推送保证,不再每分钟轮询(那会吃光 IM 的 API 月额度)。
    threading.Thread(target=reconcile_pending_once, daemon=True).start()
    print(f"[main] http listening on :{settings.PORT}, ws started + startup reconcile queued")
    uvicorn.run("app.server:app", host="0.0.0.0", port=settings.PORT, log_level="info")


if __name__ == "__main__":
    main()
