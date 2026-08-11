"""入口:启动长连接事件监听 + HTTP 服务。

长连接客户端 .start() 是阻塞的,丢后台线程跑;HTTP 用 uvicorn 在主线程跑。
"""
import threading

import lark_oapi as lark
import uvicorn

from app import feishu, settings
from app.events import build_handler, poller_loop


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
    threading.Thread(target=poller_loop, daemon=True).start()   # 对账兜底
    print(f"[main] http listening on :{settings.PORT}, ws + poller started")
    uvicorn.run("app.server:app", host="0.0.0.0", port=settings.PORT, log_level="info")


if __name__ == "__main__":
    main()
