"""Slack 审批提供方(APPROVAL_PROVIDER=slack,Socket Mode)。

模块分工(按 docs/PROVIDERS.md 的推荐布局):
  client.py     Slack Web API 薄封装(只用 5 个 bot scope)
  messaging.py  Block Kit 渲染 + 发送(审批消息 / 部署中 / 结果)
  approval.py   申请人解析、点击者鉴权(fail-close)、批准 / 拒绝(CAS)
  events.py     Socket Mode 事件入口(先 ack 后处理)+ 连接监督循环

SlackProvider 是 ApprovalProvider 契约的适配:Slack 没有平台侧的"审批实例",
实例号由 approvo 自己生成,状态以 store 为准。/release 主流程直接调用上面的模块
(与飞书路径直接调用 app.feishu 的做法一致),这里只保证契约可用。
"""
from app.providers import ApprovalProvider

# store 状态 → 契约里的平台状态词
_STATUS = {"pending": "PENDING", "deploying": "APPROVED", "success": "APPROVED",
           "failed": "APPROVED", "rejected": "REJECTED"}


class SlackProvider(ApprovalProvider):

    def subscribe(self, approval_code: str) -> None:
        """Socket Mode 无需订阅:连接建立即收得到本应用消息上的交互事件。"""

    def create_approval(self, approval_code: str, initiator: str, form_json: str,
                        uuid: str | None = None,
                        node_approver_user_id_list: list | None = None) -> str:
        """只分配实例号(slk-<uuid>);审批消息由 send_card / messaging.post_approval 另发。
        approval_code / node_approver_user_id_list 在 Slack 下无意义:审批人 = 审批频道当前成员。"""
        from app.providers.slack import approval
        return approval.new_instance_code()

    def query_status(self, instance_code: str) -> dict:
        from app import store
        rec = store.get(instance_code) or {}
        st = rec.get("status") or ""
        return {"status": _STATUS.get(st, st.upper()), "timeline": []}

    def send_card(self, chat_id: str, card: dict) -> None:
        """card = {"text": 纯文本兜底, "blocks": Block Kit 列表}。"""
        from app.providers.slack import client
        client.post_message(chat_id, card.get("text") or "", card.get("blocks") or [])

    def list_chat_members(self, chat_id: str) -> list[str]:
        from app.providers.slack import client
        return sorted(client.channel_members(chat_id))

    def user_id_by_email(self, email: str) -> str | None:
        from app.providers.slack import client
        return client.user_id_by_email(email)
