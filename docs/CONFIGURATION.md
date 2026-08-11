# Configuration Reference

approvo 配置分两层:**环境变量** (敏感凭证) + **config.yaml** (业务映射).

## 环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `FEISHU_APP_ID` | ✓ | 自建应用 App ID (`cli_xxx`);Lark 版格式相同 |
| `FEISHU_APP_SECRET` | ✓ | 自建应用 App Secret |
| `LARK_ENDPOINT` |    | `feishu` (默认, `open.feishu.cn`) / `lark` (`open.larksuite.com`) / 完整 URL (私有部署);决定 SDK 长连接 + REST API 走哪个 domain |
| `RELEASE_TOKEN` |    | `/release` 接口共享密钥;留空=不校验 (仅内网可接受) |
| `ADMIN_USER` |     | `/admin` 页 Basic Auth 用户名,默认 `admin` |
| `ADMIN_PASSWORD` |    | `/admin` 页 Basic Auth 密码;**留空即禁用整个 /admin** |
| `GITHUB_APP_ID` | (method=github 时) | GitHub App ID |
| `GITHUB_APP_OWNER` | (method=github 时) | GitHub 组织名;**无默认**,缺失即 KeyError |
| ~~`GITHUB_APP_REPO`~~ | ⚠️ deprecated | 已废弃并忽略;dispatch 目标仓改由 `config.releases.<key>.github.repo` 提供,支持一个 approvo 实例 dispatch 到多个受限仓 |
| `GITHUB_APP_PRIVATE_KEY` | (method=github 时) | App 私钥 PEM 全文 (多行) |
| `GITHUB_TOKEN` | (无 App 兜底) | PAT,`actions:write` for 受限仓;有 App 时不需要 |
| `CONFIG_PATH` |    | config.yaml 路径,默认 `config.yaml` |
| `DB_PATH` |    | SQLite 数据库路径,默认 `release.db` |
| `PORT` |    | HTTP 端口,默认 `8700` |
| `RELEASE_RATE_PER_MIN` |    | `/release` 限流,默认 `60` |

## config.yaml

```yaml
default_project: "MyProject"                # (可选) 卡片标题前缀,如 "QHSE-UMP · 发版申请 · 待审批"
                                            # 优先级: POST body.project > release.project > default_project

feishu:
  # 审批定义 - approval_code 与 approval_codes 二选一 (或都配, codes 优先):
  approval_code: "<UUID>"                   # (A) 单定义, 所有 stage 共用
  approval_codes:                           # (B) 按 stage 分派 - 推荐生产
    pre:     "<QA_UUID>"
    release: "<PROD_UUID>"                  # 生产 stage 自动 ⚠️ 围栏 + red header
    hotfix:  "<HOTFIX_UUID>"
    default: "<FALLBACK_UUID>"              # 未匹配时兜底 (可省, 会 fallback 到 approval_code)
  detail_chat_id: "oc_<...>"                # 审批群:详细申请卡去这里,动态审批人从这拉
  result_chat_id: "oc_<...>"                # 结果群:成功/失败/拒绝卡去这里
  # 或者用旧字段:
  notify_chat_id: "oc_<...>"                # 兼容旧配置;detail 和 result 都用它
  default_initiator_user_id: "<user_id>"    # 兜底申请人
  user_map:                                 # GitHub 用户名 → 飞书/Lark user_id;首次启动灌初始值
    alice: "<user_id>"                      # 老格式(name 为空)
    bob:                                    # 新格式(推荐,含真名,卡片申请人两行显示)
      user_id: "<user_id>"
      name: "Bob Smith"
  form_field_ids:                           # 仅 UI 手搓审批定义时用;脚本建的不用配
    repo: "widget_xxx"
    tag:  "widget_yyy"
    # ... 见 app/forms.py 的 KEYS

clusters:                                   # 供 method=kubectl / helm 使用
  <cluster_name>:
    kubeconfig: "/secrets/kubeconfig-xxx"   # 容器内挂载路径
    context: "<context_in_kubeconfig>"

releases:
  <release_key>:                            # 逻辑名,业务仓 curl /release 时的 repo 字段值
    method: <dryrun|kubectl|helm|github>
    project: <项目名>                        # (可选) 覆盖 default_project 的卡片前缀
    source_repo: <repo>                     # 拉变更清单;可选 (不配则跳过).只写仓库名,owner 用 GITHUB_APP_OWNER;兼容旧写法 <owner/repo>
    image_repo: <registry.example.com/org/img>   # 用于卡片展示 (image:tag 拼接)
    platform: <文本>                        # 卡片展示的"部署目标"文本
    env: <prod|test|...>                    # 卡片展示的"环境"文本;传给 deploy workflow inputs
    # ↓↓↓ method 特定字段 ↓↓↓
    # method=kubectl / helm:
    cluster: <cluster_name>                 # 引用上面 clusters 里的 key
    namespace: <k8s_ns>
    # method=kubectl:
    deployment: <k8s_deploy_name>
    container: <container_name>             # 注意 container != deployment 名的场景
    # method=helm:
    helm_release: <release_name>
    chart: </path/or/chart_ref>
    image_key: image.tag                    # --set image.tag=<tag>,默认 image.tag
    # method=github:
    github:
      owner: <github_org>
      repo: <restricted_deploy_repo>        # 受限发版仓 (每条 release 可指向不同仓)
      workflow: deploy.yml
      ref: main
      env: prod                             # 覆盖顶层 env,用于传给 deploy workflow
      inputs:                               # 可选:追加 workflow inputs
        foo: bar
    # ↓↓↓ 按 stage 覆盖 (可选) ↓↓↓
    # 让一条 release 走 pre/release/hotfix 三阶段,而不必拆成三条 release_key.
    # 卡片"应用"字段直接显示 release_key = 应用名 (符合"应用名=仓名"规矩).
    # 请求带 stage=pre → deep-merge stages.pre 到顶层 (dict 递归,其他类型 override 胜).
    stages:
      pre:
        env: qa
        platform: <文本>·QA
        github: {env: qa}                   # nested dict 会递归合并,只覆盖 env,ref/inputs 保留
      release:
        env: prod
        platform: <文本>·生产
        github: {env: prod}
      hotfix:
        env: prod
        platform: <文本>·生产·热修
        github: {env: prod}
```

## 完整示例

见 [config.example.yaml](../config.example.yaml) 项目根目录,4 种 method 各一个 release 示例.

## Stage 语义

`POST /release` body 可选 `stage` 字段 (`pre` / `release` / `hotfix` / 自定义),用于:

1. **审批定义分派** — 见 `feishu.approval_codes` 配置,不同 stage 走不同审批人组合
2. **卡片视觉强化** — `stage in ("release", "hotfix")` 或 `env in ("prod", "production")` 时:
   - Header template 自动切 `red`(申请卡) / 结果卡各态颜色不变但加"生产"prefix
   - 卡片顶部 + 底部各加一行 ⚠️ 装饰围栏 (24 个 emoji 占满宽度)
   - 提示语从"审批人=群成员"改为"生产发版,审批人=指定负责人"

未传 `stage` 或传空 → 视为 pre (向后兼容).

## 常见配置陷阱

- **`replicaCount` != 1**:chart 会 fail;SQLite 单副本硬约束
- **`GITHUB_APP_OWNER` 未设**:approvo 启动崩 KeyError (**这是设计**,防误接前公司组织)
- **`ADMIN_PASSWORD` 未设**:`/admin` 整体禁用,GitHub 用户映射只能通过首次启动灌 `user_map` 或直接改 SQLite
- **`container` 字段错**:kubectl set image 会报 `container xxx not found`;chart 生成的 pod 里容器名可能带前缀
- **`github.ref` 拼错**:workflow_dispatch 会 422;approvo 在日志里能看到
- **`source_repo` 不写**:变更清单区块完全不渲染 (不报错)
- **飞书审批定义控件顺序变了**:approvo 按 `form_field_ids` 里的 id 匹配,和顺序无关;但控件 id 错会导致值绑不上
