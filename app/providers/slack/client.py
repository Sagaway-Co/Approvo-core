"""Slack Web API 的薄封装(官方 slack_sdk.WebClient)。对应飞书侧的 app/feishu.py。

只用 5 个 bot scope 覆盖得到的方法 —— 加方法前先确认它不需要新 scope:
  chat:write        chat.postMessage / chat.update / chat.postEphemeral
  channels:read     conversations.info / conversations.members(公开频道)
  groups:read       同上(私有频道)
  users:read        users.info(是不是机器人、显示名)
  users:read.email  users.lookupByEmail(operator_email → Slack 用户)
另有 auth.test(校验 bot token 本身,任何 token 都能调,不需要 scope),
以及交互负载自带的 response_url(Slack 为每次点击签发的回调地址,同样不需要 scope)。

🔴 与 feishu.get_chat_members 的一个刻意差异:这里的成员查询【失败就抛】,
   绝不返回半截名单。它是审批人鉴权的判据,判据拿不全时调用方必须按拒绝处理。
"""
import threading

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app import settings

_web: WebClient | None = None
_lock = threading.Lock()

# conversations.members 单页 200,50 页 = 1 万人。超过就说明频道选错了(审批频道不该是全员群),
# 与其拉一个不完整的名单去判定,不如直接拒绝。
_MEMBERS_PAGE = 200
_MEMBERS_MAX_PAGES = 50


def web() -> WebClient:
    """懒初始化(同 store._get_pool 的双重检查):import 不该连网,也不该要求 token 已就绪。"""
    global _web
    if _web is None:
        with _lock:
            if _web is None:
                _web = WebClient(token=settings.SLACK_BOT_TOKEN, timeout=10)
    return _web


def auth_test() -> dict:
    """校验 bot token 是否有效(无效则 SlackApiError);返回机器人自己的 user_id / team_id。"""
    r = web().auth_test()
    return {"user_id": r.get("user_id"), "team_id": r.get("team_id")}


def channel_info(channel: str) -> dict:
    return dict(web().conversations_info(channel=channel)["channel"])


def channel_members(channel: str) -> set[str]:
    """频道【当前】成员 user ID 集合(分页拉全)。

    🔴 刻意【不缓存】:踢出频道 = 立即撤销审批权。哪怕缓存 60 秒,
       被踢的人在这 60 秒里仍然能批生产 —— 而审批点击本来就是低频事件,不差这一次调用。
    🔴 任何一页失败都直接抛(SlackApiError / 网络异常),不吞、不返回部分结果。
    """
    members: set[str] = set()
    cursor = ""
    for _ in range(_MEMBERS_MAX_PAGES):
        kw = {"channel": channel, "limit": _MEMBERS_PAGE}
        if cursor:
            kw["cursor"] = cursor
        resp = web().conversations_members(**kw)
        members.update(resp.get("members") or [])
        cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor:
            return members
    raise RuntimeError(f"频道成员超过 {_MEMBERS_PAGE * _MEMBERS_MAX_PAGES} 人(分页未结束),"
                       f"拒绝按不完整名单判定 —— 审批频道应是一个小范围的专用频道")


def user_info(user: str) -> dict:
    return dict(web().users_info(user=user)["user"])


def display_name(user: dict) -> str:
    """显示名优先,其次真名,再次用户名。只用于展示与审计日志,不参与任何判定。"""
    prof = user.get("profile") or {}
    return (prof.get("display_name") or prof.get("real_name")
            or user.get("real_name") or user.get("name") or "")


def user_id_by_email(email: str) -> str | None:
    """邮箱反查 Slack user ID。查无此人返回 None;其它错误照常抛,由调用方决定怎么降级。"""
    try:
        return web().users_lookupByEmail(email=email)["user"]["id"]
    except SlackApiError as e:
        if (e.response or {}).get("error") == "users_not_found":
            return None
        raise


def post_message(channel: str, text: str, blocks: list) -> tuple[str, str]:
    """发消息,返回 (channel, ts)。text 是通知 / 无障碍用的纯文本兜底(Slack 要求有)。"""
    r = web().chat_postMessage(channel=channel, text=text, blocks=blocks,
                               unfurl_links=False, unfurl_media=False)
    return r["channel"], r["ts"]


def update_message(channel: str, ts: str, text: str, blocks: list) -> None:
    web().chat_update(channel=channel, ts=ts, text=text, blocks=blocks)


def post_thread_reply(channel: str, thread_ts: str, text: str) -> None:
    """在某条消息的线程里回复(不在频道里另起一条)。用于单卡模式下的失败提醒:
    改消息不会推送通知,线程回复会 —— 失败需要有人被叫到。"""
    web().chat_postMessage(channel=channel, thread_ts=thread_ts, text=text,
                           unfurl_links=False, unfurl_media=False)


def post_ephemeral(channel: str, user: str, text: str) -> None:
    """只有 user 能看见的提示(审批被拒的原因等)。不进频道历史,不打扰其他人。
    ⚠️ 对方不在该频道时 Slack 会报 user_not_in_channel —— 所以优先用 respond_ephemeral。"""
    web().chat_postEphemeral(channel=channel, user=user, text=text)


_RESPONSE_URL_PREFIX = "https://hooks.slack.com/"


def respond_ephemeral(response_url: str, text: str) -> None:
    """经交互负载里的 response_url 给点击者回一条仅自己可见的消息。

    为什么优先用它:被拒最多的恰恰是【非频道成员】(公开频道里谁都能点到按钮),
    而 chat.postEphemeral 发不给不在频道里的人 —— 那样他点了没任何反馈,只会以为机器人坏了。
    response_url 是 Slack 为这次点击签发的一次性回调地址,不需要任何 scope。
    🔴 只认 Slack 自己的域名:地址来自负载,不验形状就等于"负载让我往哪发我就往哪发"。
    """
    if not str(response_url or "").startswith(_RESPONSE_URL_PREFIX):
        raise ValueError("response_url 不是 Slack 的回调地址")
    from slack_sdk.webhook import WebhookClient
    r = WebhookClient(response_url, timeout=10).send(
        text=text, response_type="ephemeral", replace_original=False)
    if r.status_code != 200:
        raise RuntimeError(f"response_url 回复失败 HTTP {r.status_code}: {str(r.body)[:100]}")
