"""FeishuProvider - ApprovalProvider 的飞书实现.

薄 wrapper:方法体直接调用现有 app.feishu 模块的函数,保证 A3 阶段零逻辑改动.
后续可将 app/feishu.py 的实现内联进本文件,让 provider 完全自包含.
"""
from app import feishu
from app.providers import ApprovalProvider


class FeishuProvider(ApprovalProvider):

    def subscribe(self, approval_code: str) -> None:
        feishu.subscribe(approval_code)

    def create_approval(self, approval_code: str, initiator: str, form_json: str,
                        uuid: str | None = None,
                        node_approver_user_id_list: list | None = None) -> str:
        return feishu.create_instance(
            approval_code, initiator, form_json,
            uuid=uuid, node_approver_user_id_list=node_approver_user_id_list,
        )

    def query_status(self, instance_code: str) -> dict:
        return feishu.get_instance(instance_code)

    def send_card(self, chat_id: str, card: dict) -> None:
        feishu.send_card(chat_id, card)

    def list_chat_members(self, chat_id: str) -> list[str]:
        return feishu.get_chat_members(chat_id)

    def user_id_by_email(self, email: str) -> str | None:
        return feishu.user_id_by_email(email)
