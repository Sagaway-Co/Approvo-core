# Approval Providers

Approvo 通过 `ApprovalProvider` ABC 支持替换审批平台,由环境变量 `APPROVAL_PROVIDER` 选择
(`feishu` 默认 / `slack`;未知取值拒绝启动).当前内置:

| Provider | 状态 | 特性 |
| --- | --- | --- |
| feishu (飞书) | ✅ Ready | 长连接、动态审批人、审批实例、消息卡片 |
| slack | ✅ Ready | Socket Mode(只出站)、Block Kit 审批消息 + 批准/拒绝按钮、审批人 = 审批频道当前成员、禁止自批、按 stage 白名单。见 [SLACK.md](SLACK.md) |
| wecom (企微) | 🚧 未实现 | 参考 another-app 旧实现(WeCom OA API + FC webhook) |
| dingtalk (钉钉) | 🚧 未实现 | 类似飞书但 API 有差异 |

## Slack provider 要点

代码在 [`app/providers/slack/`](../app/providers/slack/),布局即下文"新增 Provider 步骤"推荐的那套
(`client` / `messaging` / `approval` / `events`)。与飞书的根本差异:

- **没有平台侧的"审批实例"**。实例号由 approvo 生成(`slk-<uuid>`),审批状态只存在 approvo 自己的库里;
  因此不需要订阅、也不需要启动对账 —— 不存在"平台上已决策、我们没收到"的情况
  (连接不在时点击会在 Slack 端当场失败,状态保持 pending,重连后可再点)。
- **"谁能批"不再由平台把关,全部落在 approvo 的点击鉴权上**,而且是 fail-close 的:
  审批频道当前成员(不缓存)、非机器人、频道非外部共享、可选的 stage 白名单、默认禁止自批。
  规则与"申请人映射不出来"时的策略见 [SLACK.md](SLACK.md#审批规则)。
- **状态机与部署完全复用**:批准 = `store.try_claim_for_deploy`(pending→deploying),
  拒绝 = `store.try_transition`(pending→rejected),两个都是 CAS;部署走 `events._run_deploy`,
  与飞书 / 审批直通是同一个函数,只是通知发到 Slack。
- **Socket Mode 事件先 ack 再处理**(Slack 要求 3 秒内确认),处理丢后台线程。
- **连接监督循环**与飞书长连接同构:崩了退避重建(2s 起、指数、上限 300s,稳定 ≥60s 后归零),
  健康状态进同一个 `wshealth`(`/healthz`、`/readyz`、`/release` 的 fail-close 判据不变),
  日志关键字仍是 `[ws][down]`(附 `provider=slack`)。每次建连前自检 bot token 与频道,
  因为这两样坏了 Socket Mode 照样连得上。
- 飞书专属端点(只读凭证、Secret 变更、发版密钥、`/admin` 映射页)在 slack 模式下返回 **501**。

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
- **事件监听**:飞书长连接绑定在 `app/main.py` 里直接用 lark-oapi SDK;Slack 的 Socket Mode 监督循环在
  `app/providers/slack/events.py`,由 `app/main.py` 按 `APPROVAL_PROVIDER` 选择启动。
  后续可统一成 ABC 上的 `run_event_listener()` 方法

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
    from app import settings                 # APPROVAL_PROVIDER 在 settings 里校验过
    if settings.APPROVAL_PROVIDER == "slack":
        from app.providers.slack import SlackProvider
        return SlackProvider()
    if settings.APPROVAL_PROVIDER == "dingtalk":   # 新 provider:同时把取值加进 settings 的白名单
        from app.providers.dingtalk import DingtalkProvider
        return DingtalkProvider()
    from app.providers.feishu import FeishuProvider
    return FeishuProvider()
```

### 4. 事件监听

不同平台事件订阅方式差异大:
- 飞书:长连接 (lark-oapi SDK,出方向)
- 钉钉:HTTP webhook (需公网入口,provider 提供 verify + decrypt)
- 企微:HTTP webhook (类似钉钉,加密 XML)
- Slack:Socket Mode (出方向,已实现,见 `app/providers/slack/events.py`)

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
