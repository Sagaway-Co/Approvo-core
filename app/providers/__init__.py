"""审批平台适配器抽象层.

approvo 目前仅内置飞书 provider,后续可扩展为钉钉 / Slack / 企微 等.
新增 provider 请见 docs/PROVIDERS.md.
"""
from abc import ABC, abstractmethod


class ApprovalProvider(ABC):
    """审批平台契约.

    实现类应封装:
    - 创建/查询审批实例
    - 发消息卡片到群
    - 拉群成员(动态审批人)
    - 邮箱反查 user_id
    - 订阅+监听事件(长连接或 webhook)
    """

    @abstractmethod
    def subscribe(self, approval_code: str) -> None:
        """启动时调用一次,幂等."""

    @abstractmethod
    def create_approval(self, approval_code: str, initiator: str, form_json: str,
                        uuid: str | None = None,
                        node_approver_user_id_list: list | None = None) -> str:
        """创建审批实例,返回 instance_code."""

    @abstractmethod
    def query_status(self, instance_code: str) -> dict:
        """查询审批实例详情,返回 dict(至少含 status / timeline)."""

    @abstractmethod
    def send_card(self, chat_id: str, card: dict) -> None:
        """发消息卡片到群."""

    @abstractmethod
    def list_chat_members(self, chat_id: str) -> list[str]:
        """获取群当前成员的 user_id 列表(动态审批人用)."""

    @abstractmethod
    def user_id_by_email(self, email: str) -> str | None:
        """邮箱反查 user_id;查不到返回 None."""


def get_provider() -> ApprovalProvider:
    """provider 工厂.当前仅支持飞书;后续按 settings.PROVIDER 分派."""
    from app.providers.feishu import FeishuProvider
    return FeishuProvider()
