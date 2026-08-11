# Approval Providers

Approvo 通过 `ApprovalProvider` ABC 支持替换审批平台.当前内置:

| Provider | 状态 | 特性 |
| --- | --- | --- |
| feishu (飞书) | ✅ Ready | 长连接、动态审批人、审批实例、消息卡片 |
| wecom (企微) | 🚧 未实现 | 参考 another-app 旧实现(WeCom OA API + FC webhook) |
| dingtalk (钉钉) | 🚧 未实现 | 类似飞书但 API 有差异 |
| slack | 🚧 未实现 | 交互按钮 + Events API 或 Socket Mode |

## ApprovalProvider 契约

定义在 [`app/providers/__init__.py`](../app/providers/__init__.py):

```python
class ApprovalProvider(ABC):
    @abstractmethod
    def subscribe(self, approval_code: str) -> None: ...

    @abstractmethod
    def create_approval(self, approval_code: str, initiator: str, form_json: str,
                        uuid: str | None = None,
                        node_approver_user_id_list: list | None = None) -> str: ...

    @abstractmethod
    def query_status(self, instance_code: str) -> dict: ...

    @abstractmethod
    def send_card(self, chat_id: str, card: dict) -> None: ...

    @abstractmethod
    def list_chat_members(self, chat_id: str) -> list[str]: ...

    @abstractmethod
    def user_id_by_email(self, email: str) -> str | None: ...
```

### 契约要点

- **`create_approval`** 返回 `instance_code`(字符串),后续所有交互靠这个 id
- **`query_status`** 返回 dict,至少含 `status` (`APPROVED` / `REJECTED` / `CANCELED` / `DELETED` / `PENDING` 等) 与 `timeline` (审批事件列表)
- **`send_card`** 卡片格式当前是飞书 msg card json;后续可能抽象出 `Card` DTO,provider 自己渲染
- **`list_chat_members`** 返回 user_id 列表,过滤掉机器人 / 无 user_id 的成员
- **事件监听**:目前长连接绑定在 `app/main.py` 里直接用 lark-oapi SDK;后续应移到 provider 内部,ABC 增加 `run_event_listener()` 方法

## 新增 Provider 步骤

以钉钉为例:

### 1. 建目录

```
app/providers/dingtalk/
├── __init__.py
├── client.py       # 基础 HTTP + token 缓存
├── approval.py     # 创建/查询审批实例
├── messaging.py    # 发消息卡片、拉群成员
└── events.py       # webhook / 长连接事件回调
```

### 2. 实现 ApprovalProvider

```python
# app/providers/dingtalk/__init__.py
from app.providers import ApprovalProvider

class DingtalkProvider(ApprovalProvider):
    def create_approval(self, ...) -> str:
        # 调钉钉 OpenAPI
        ...
```

### 3. 注册到 factory

修改 [`app/providers/__init__.py`](../app/providers/__init__.py):

```python
def get_provider() -> ApprovalProvider:
    provider_type = os.environ.get("APPROVAL_PROVIDER", "feishu")
    if provider_type == "feishu":
        from app.providers.feishu import FeishuProvider
        return FeishuProvider()
    if provider_type == "dingtalk":
        from app.providers.dingtalk import DingtalkProvider
        return DingtalkProvider()
    raise ValueError(f"unknown provider: {provider_type}")
```

### 4. 事件监听

不同平台事件订阅方式差异大:
- 飞书:长连接 (lark-oapi SDK,出方向)
- 钉钉:HTTP webhook (需公网入口,provider 提供 verify + decrypt)
- 企微:HTTP webhook (类似钉钉,加密 XML)
- Slack:HTTP Events API 或 Socket Mode (推荐 Socket Mode,出方向)

如果 provider 走 webhook,建议加一个 FastAPI 路由 `/webhook/<provider>`,在 provider 内部注册路由:

```python
# app/providers/dingtalk/events.py
from fastapi import Request
from app.events import process_instance

async def dingtalk_webhook(req: Request):
    body = await req.body()
    ic = verify_and_extract_instance_code(body)
    process_instance(ic)
```

在 `app/server.py` 注册路由(或者用 `app.include_router`).

### 5. 测试

- 用 provider 提供的 mock endpoint 联调 (钉钉/企微都有 sandbox)
- 用 dryrun deployer 跑通 create → send_card → 审批 → get_instance → 结果卡完整链路
- 边界:被拒 / 撤销 / 长连接断线 / 重复事件投递 / 群成员空

## 卡片抽象 (未来)

目前 `cards.py` 生成的是**飞书专属**的 msg card JSON.后续应抽象出一个 provider-neutral 的 `Card` DTO,让 provider 自己渲染成对应平台的卡片/消息.

粗略草案:

```python
class Card:
    header: CardHeader   # {template: green/red/orange, title}
    fields: dict          # 应用/环境/版本/申请人/...
    body_md: str          # 主体 markdown
    footnote: str

class ApprovalProvider(ABC):
    def render_card(self, card: Card) -> dict: ...
```

这不在 v0.1 范围内.
