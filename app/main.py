"""入口:启动长连接事件监听 + HTTP 服务。

长连接客户端 .start() 是阻塞的,丢后台线程跑;HTTP 用 uvicorn 在主线程跑。
"""
import threading

import lark_oapi as lark
import uvicorn

from app import feishu, settings
from app.events import build_handler, reconcile_pending_once


def _run_ws():
    cli = lark.ws.Client(
        settings.APP_ID, settings.APP_SECRET,
        domain=settings.LARK_DOMAIN,     # feishu 或 lark endpoint(见 settings.py)
        event_handler=build_handler(),
        log_level=lark.LogLevel.INFO,
    )
    cli.start()  # 阻塞 + 自动断线重连


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
