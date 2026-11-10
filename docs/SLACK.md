# Slack 审批:创建 Slack App

`APPROVAL_PROVIDER=slack` 时,approvo 在 Slack 审批频道发一条带「批准 / 拒绝」按钮的审批消息,
按钮点击经 **Socket Mode** 送回 approvo。approvo 只**出站**连 Slack(WSS + HTTPS),
**不需要**公网回调地址,也不需要配 Request URL / Signing Secret。

全部配置约 10 分钟。本页只讲 Slack 侧;approvo 侧的字段见
[CONFIGURATION.md](CONFIGURATION.md#slack-审批-approval_providerslack),
审批规则(谁能批、自批怎么判)见下文[审批规则](#审批规则)。

## 1. 建 App(推荐用 manifest)

打开 <https://api.slack.com/apps> → **Create New App** → **From an app manifest** →
选你的工作区 → 粘贴下面这段(名字可改):

```yaml
display_information:
  name: approvo
features:
  bot_user:
    display_name: approvo
    always_online: true
oauth_config:
  scopes:
    bot:
      - chat:write          # 发审批消息、更新消息、给点击者发仅自己可见的提示
      - channels:read       # 查公开频道信息 / 成员(审批人 = 审批频道当前成员)
      - groups:read         # 同上,私有频道
      - users:read          # 查点击者是不是机器人、显示名
      - users:read.email    # 按 operator_email 把申请人映射到 Slack 用户
settings:
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
  org_deploy_enabled: false
  token_rotation_enabled: false
```

⚠️ **只要这 5 个 bot scope**,不要多加。approvo 不读消息内容、不发私信、不改频道。
多一个 scope 就多一份 token 泄漏时的损失面。

手动创建也可以:**Socket Mode** 打开、**Interactivity & Shortcuts** 打开(Socket Mode 下无需填 URL)、
**OAuth & Permissions → Bot Token Scopes** 只加上面 5 个。

## 2. 拿两个 token(都只放环境变量)

| 环境变量 | 在哪拿 | 形如 | 用途 |
| --- | --- | --- | --- |
| `SLACK_APP_TOKEN` | **Basic Information → App-Level Tokens → Generate**,scope 选 `connections:write` | `xapp-...` | Socket Mode 建连 |
| `SLACK_BOT_TOKEN` | **OAuth & Permissions → Install to Workspace** 后的 *Bot User OAuth Token* | `xoxb-...` | Web API |

```bash
kubectl -n approvo create secret generic approvo-secret \
  --from-literal=APPROVAL_PROVIDER=slack \
  --from-literal=SLACK_BOT_TOKEN='xoxb-...' \
  --from-literal=SLACK_APP_TOKEN='xapp-...' \
  --from-literal=RELEASE_TOKEN=... # 以及 DB_DSN / GITHUB_APP_* 等,见 CONFIGURATION.md
```

🔴 不要写进 config.yaml、values 文件或任何会进 git 的地方。启动时缺任何一个、或前缀不对
(比如误把 `xoxp-` 用户 token 当 bot token),approvo **直接拒绝启动**。

## 3. 准备频道

1. 建一个**只放审批人**的频道(建议私有),例如 `#release-approval`。
   **审批人 = 这个频道的当前成员**:拉进来 = 授权,踢出去 = 立即撤权(成员每次点击实时查,不缓存)。
2. 在频道里 `/invite @approvo` 把机器人拉进来。结果频道若是另一个频道,同样要拉。
3. 拿频道 ID:频道名 → *View channel details* → 最底部 `Channel ID`(形如 `C0XXXXXXX`)。
   approvo **只认 ID,不认 `#频道名`**(改名不改 ID)。

⚠️ 审批频道**不能**是 Slack Connect 外部共享频道 —— 外部组织的成员也会出现在成员列表里。
approvo 检测到外部共享会把审批链路判为不可用(`/readyz` 503、`/release` 拒绝受理),点击也一律拒绝。
机器人没被拉进频道、或 bot token 失效时同样如此 —— 这些情况下 Socket Mode 本身照样连得上,
所以 approvo 每次建连前都会先自检,而不是等到第一次发消息失败才暴露。

## 4. 拿用户 ID(用于 user_map / approvers)

个人资料 → `⋮` → *Copy member ID*(形如 `U0XXXXXXX`)。

## 5. 填 config.yaml

```yaml
slack:
  approval_channel: "C0XXXXXXX"      # 审批频道 ID(必填)
  result_channel: "C0YYYYYYY"        # 结果频道 ID(可省,默认同审批频道)
  forbid_self_approval: true         # 禁止自批(默认 true)
  user_map:                          # GitHub 登录名 → Slack 用户
    alice: "U0XXXXXXX"
    bob: {user_id: "U0YYYYYYY", name: "Bob"}
  # approvers:                       # (可选)按 stage 限定审批人,配了就必须【同时】满足
  #   release: ["U0XXXXXXX", "U0ZZZZZZZ"]
  #   default: ["U0XXXXXXX"]         # 未单列的 stage 用它;不写 default 则未单列的 stage 不限定
```

`feishu:` 段在 slack 模式下不需要(有也不读)。

启动后看日志里的 `[slack][up] Socket Mode 已连接`,或 `GET /readyz` 返回 200。
连不上时日志是 `[ws][down] provider=slack ... err=...`(与飞书模式同一个告警关键字)。

## 审批规则

点击时**实时**校验,任何一步查不到都按**拒绝**处理(点击者会收到一条仅自己可见的原因说明,状态不变):

1. 点的是审批频道里的审批消息;
2. 点击者是真人(不是 bot / app 用户),账号未停用;
3. 审批频道不是外部共享频道;
4. 点击者是审批频道**当前**成员;
5. (仅批准)该 stage 配了 `approvers` → 必须在名单里;
6. (仅批准)`forbid_self_approval: true` → 不能是申请人本人。

**拒绝**只要求 1~4:拒绝是收紧,任何成员都可以拒,申请人也可以撤回自己的申请。

### 申请人怎么映射、映射不出来怎么办

申请人的 Slack 身份 = 下列来源的**并集**(多算一个只会让自批检查更严):
`slack.user_map[github_actor]`、请求里的 `operator_user_id`(须是 Slack user ID)、
`users.lookupByEmail(operator_email)`。

映射不出来时 approvo **照常受理**(消息上标「未映射」),但批准规则收紧为 fail-close:

| 情况 | 谁能批准 |
| --- | --- |
| 申请人映射到了 Slack 用户 | 除申请人以外的成员 |
| 有 `github_actor` 但没映射上 | 只有**在 `user_map` 里登记过**、且登记的 GitHub 账号不是该申请人的成员 —— 没登记的人可能恰好就是申请人 |
| 连 `github_actor` 都没有、邮箱也查不到 | **谁都不能批**;请拒绝,补全身份后重新提交 |

⚠️ 已知边界:同一个人有多个 GitHub 账号时,请把所有账号都登记到同一个 Slack 用户下,
否则他用未登记的账号发起、再用已登记的身份批准,规则拦不住。
另外申请人身份来自 CI 上报(`github_actor` 等),它的可信度等于调用方 workflow 的可信度 ——
这与飞书模式相同。

## slack 模式下不支持的功能

以下端点依赖飞书身份(群成员 / 机器人菜单),slack 模式下一律返回 **501** 并说明原因:
`/viewer-token`、`/viewer-panel`、`/secret/request`、`/secret/commit`、`/release-key`、
`/admin` 与 `/api/usermap`、`/api/resolve`(slack 模式的申请人映射在 `slack.user_map`,改动走 git 审查)。

与 IM 无关的通道照常工作:`/release`、`/healthz`、`/readyz`、`/deploy-credential`(+ `/revoke`)、
审批直通 `APPROVAL_BYPASS`(通知发到 Slack 结果频道)。
