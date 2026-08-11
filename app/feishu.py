"""飞书 REST 调用:tenant_access_token 缓存、创建审批实例、订阅事件、发卡片、邮箱转 user_id。

事件监听不在这里(走 lark-oapi 的长连接,见 events.py / main.py)。
"""
import json
import os
import threading
import time

import requests

from app import settings

BASE = f"{settings.LARK_DOMAIN}/open-apis"

_token = {"value": None, "exp": 0.0}
_token_lock = threading.Lock()


def tenant_token() -> str:
    with _token_lock:
        if _token["value"] and time.time() < _token["exp"] - 60:
            return _token["value"]
        app_id = os.environ.get("FEISHU_APP_ID")
        app_secret = os.environ.get("FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量")
        r = requests.post(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"get tenant_access_token failed: {d}")
        _token["value"] = d["tenant_access_token"]
        _token["exp"] = time.time() + int(d["expire"])
        return _token["value"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {tenant_token()}",
        "Content-Type": "application/json; charset=utf-8",
    }


def subscribe(approval_code: str) -> None:
    """对一个审批定义订阅事件。同一应用只需一次;重复订阅飞书返回非 0 也无害。"""
    r = requests.post(
        f"{BASE}/approval/v4/approvals/{approval_code}/subscribe",
        headers=_headers(),
        timeout=10,
    )
    d = r.json()
    if d.get("code") not in (0, 1390007):  # 1390007 ≈ 已订阅
        # 不致命:打印告警,启动继续
        print(f"[feishu] subscribe warn: {d}")
    else:
        print(f"[feishu] subscribed approval_code={approval_code} ok")


def create_instance(approval_code: str, initiator: str, form_json: str,
                    uuid: str | None = None,
                    node_approver_user_id_list: list | None = None) -> str:
    """创建审批实例,返回 instance_code。initiator 可为 user_id 或 open_id(ou_ 前缀自动识别;
    SSO 登录同步进来的用户往往只有 open_id)。form_json 是已 json.dumps 的控件值数组。"""
    body = {"approval_code": approval_code, "form": form_json}
    if initiator.startswith("ou_"):
        body["open_id"] = initiator
    else:
        body["user_id"] = initiator
    if uuid:
        body["uuid"] = uuid
    if node_approver_user_id_list:
        body["node_approver_user_id_list"] = node_approver_user_id_list
    r = requests.post(f"{BASE}/approval/v4/instances", headers=_headers(),
                      json=body, timeout=15)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"create instance failed: {d}")
    return d["data"]["instance_code"]


def get_instance(instance_code: str) -> dict:
    r = requests.get(f"{BASE}/approval/v4/instances/{instance_code}?user_id_type=user_id",
                     headers=_headers(), timeout=10)
    return r.json().get("data", {})


def user_id_by_email(email: str) -> str | None:
    r = requests.post(
        f"{BASE}/contact/v3/users/batch_get_id?user_id_type=user_id",
        headers=_headers(), json={"emails": [email]}, timeout=10,
    )
    d = r.json()
    if d.get("code") != 0:
        print(f"[feishu] batch_get_id warn: {d}")
        return None
    for u in d.get("data", {}).get("user_list", []):
        if u.get("user_id"):
            return u["user_id"]
    return None


def get_chat_members(chat_id: str, member_id_type: str = "user_id") -> list[str]:
    """群当前成员的 ID 列表(过滤掉无该 ID 的,如机器人)。审批人=群成员用。

    🔴 member_id_type 必须与【比对方拿到的 ID 类型】一致,否则比对恒假、拒绝所有人。
    卡片回调的 operator 里 open_id 恒有,而 user_id 需应用具备"获取用户 user ID"权限
    才返回 —— 所以卡片那条路要按实际拿到的类型来查群成员,不能写死 user_id。
    """
    ids, token = [], None
    while True:
        params = {"member_id_type": member_id_type, "page_size": 100}
        if token:
            params["page_token"] = token
        r = requests.get(f"{BASE}/im/v1/chats/{chat_id}/members",
                         headers=_headers(), params=params, timeout=10)
        d = r.json()
        if d.get("code") != 0:
            print(f"[feishu] get_chat_members warn: {d}")
            break
        for m in d.get("data", {}).get("items", []):
            if m.get("member_id"):
                ids.append(m["member_id"])
        if d.get("data", {}).get("has_more"):
            token = d["data"].get("page_token")
        else:
            break
    return ids


def user_name_by_id(uid: str, id_type: str = "open_id") -> str | None:
    """按 open_id/user_id 反查真名。

    🔴 为什么需要它：机器人菜单事件里的 operator_name 常常是【空的】
    （应用没有通讯录权限时就取不到），而"谁取用了凭证/密钥"必须让人看懂 ——
    群里公示一串 e1231g4g 等于没有归因，而可归因正是这套自助通道的立身之本。
    查不到不报错、由调用方兜底，通知绝不能阻断主流程。
    """
    if not uid:
        return None
    try:
        r = requests.get(f"{BASE}/contact/v3/users/{uid}",
                         headers=_headers(), params={"user_id_type": id_type}, timeout=10)
        d = r.json()
        if d.get("code") == 0:
            return ((d.get("data") or {}).get("user") or {}).get("name")
        print(f"[feishu] user_name_by_id warn: code={d.get('code')} msg={d.get('msg')}"
              f"（多半是缺 contact:user.base:readonly 权限）")
    except Exception as e:  # 查名字失败绝不能影响签发
        print(f"[feishu] user_name_by_id error: {type(e).__name__}: {e}")
    return None


def send_text(receive_id: str, text: str, receive_id_type: str = "open_id") -> tuple[bool, str]:
    """发纯文本消息。用于私聊投递 kubeconfig 这类【长且需原样复制】的内容。

    🔴 为什么不用卡片:卡片的 lark_md 不支持 ``` 围栏代码块(会把反引号字面显示),
    而 kubeconfig 有 3KB 且必须逐字符原样,放进卡片会被渲染破坏。text 消息不做 markdown
    渲染,原样送达可整段复制。
    🔴 返回 (ok, err):投递失败必须让调用方知道 —— 否则人点了按钮却什么也没收到,
    而群里已经公示"已签发",看起来像成功。
    """
    if not receive_id:
        return False, "receive_id 为空"
    try:
        r = requests.post(
            f"{BASE}/im/v1/messages?receive_id_type={receive_id_type}",
            headers=_headers(),
            json={"receive_id": receive_id, "msg_type": "text",
                  "content": json.dumps({"text": text}, ensure_ascii=False)},
            timeout=10,
        )
        d = r.json()
    except Exception as e:  # 网络异常也算投递失败(BLE001 已在 ruff.toml 全局 ignore)
        return False, f"{type(e).__name__}: {e}"
    if d.get("code") != 0:
        # 常见:私聊需要 im:message 相关权限,未授时这里会明确报错
        print(f"[feishu] send_text warn: {d}")
        return False, f"code={d.get('code')} msg={d.get('msg')}"
    return True, ""


def send_card(receive_id: str, card: dict, receive_id_type: str = "chat_id") -> None:
    if not receive_id:
        return
    r = requests.post(
        f"{BASE}/im/v1/messages?receive_id_type={receive_id_type}",
        headers=_headers(),
        json={"receive_id": receive_id, "msg_type": "interactive",
              "content": json.dumps(card, ensure_ascii=False)},
        timeout=10,
    )
    d = r.json()
    if d.get("code") != 0:
        print(f"[feishu] send_card warn: {d}")
