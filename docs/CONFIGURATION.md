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
| `RELEASE_RATE_PER_MIN` |    | 每个端点每分钟的正常业务额度,默认 `60`(**按端点独立分桶**,一个端点被刷不连坐其它端点) |
| `REJECT_RATE_PER_MIN` |    | 认证失败的**独立拒绝桶**额度,默认 `600`;错 token / 错口令只消耗它,不吃正常业务额度,但仍有天花板 |
| `ENABLE_DOCS` |    | 置 `1`/`true`/`yes` 才暴露 `/docs`、`/redoc`、`/openapi.json`;**默认关**(fail-close) |
| `PUBLIC_GATE_URL` |    | 对外入口地址,**仅**用于在提示信息里拼可复制的 curl,不参与鉴权;不给真实默认值 |
| `APPROVAL_BYPASS` |    | 审批直通模式;值为授权月 `YYYY-MM`(当月生效、跨月自动失效)。详见下方[审批直通模式](#审批直通模式-approval-bypass) |

## 为什么接口文档默认关

`/openapi.json`、`/docs`、`/redoc` 是 FastAPI 自动生成的,**无需任何认证即可访问**,
会把全部端点定义、请求模型字段以及**代码里的默认值**一次性交出去 ——
包括部署目标的集群名与 namespace。它不构成"直接可利用漏洞",
但把攻击者的探测成本降到接近零。本地开发要看文档时设 `ENABLE_DOCS=1`。

## 限流的形状(比额度数字更重要)

- **按端点分桶**:`/release` 被刷不会让 `/viewer-token`、`/secret/*`、`/release-key` 一起不可用。
  原实现是**一个全局桶**,于是单个匿名攻击者用错 token 刷 `/release` 就能让整个网关瘫掉。
- **认证失败走独立拒绝桶**:未认证请求不与正常业务抢额度;但拒绝桶本身有上限,
  给暴力尝试(尤其 `/admin` 的 Basic Auth,用户名默认是已知的 `admin`)留一个天花板。

## 审批直通模式 (approval bypass)

`APPROVAL_BYPASS` 让 `/release` 在【不调用 IM API】的前提下继续放行发版,用于"审批通道
暂时不可用、但仍需发版"的受控场景(典型:IM 开放平台的 API 月额度耗尽 —— 建审批实例 /
拉群成员 / 发卡三处调用全部失败,审批发不出去)。它是一个**受支持的运行期功能**,
而非一次性 hack:行为、失效时机、安全边界都确定且可预期。

**取值**:授权月 `YYYY-MM`(例 `2026-08`)。空 / 缺省 / 任何其它格式(含 `1`/`true`)
一律视为关闭(fail-close)。

**按环境非对称分流**(env 取自 `req.env` > `release.env` > `github.env`):

| 环境 | 行为 |
| --- | --- |
| `env=qa` | 跳过审批,直接执行【原有部署流程】(落 pending → 原子认领 → 部署 → 发结果卡),即"秒通过" |
| `env≠qa`(生产 / env 缺失) | **只落库留痕 + 状态置 `failed` + 不部署**,由人工线下执行。生产绝不静默上一个未经审批的版本 |

**月度自动失效**:IM 的 API 额度通常按自然月重置,故开关【自带过期】—— 到下月 1 号 0 点,
当前月 ≠ 授权月即自动回到"需审批"(生产也一并恢复)。判月用配置的时区(`Asia/Shanghai`),
每次调用实时取当前月,长驻进程跨月也如实失效;要继续用必须显式改成新月份。

**为什么是环境变量而非 config.yaml**:它是运行期开关,要能脱离业务配置快速开合,且刻意
不常驻进 git 里的环境 values —— 避免忘记关而把审批门禁长期敞着;跨月自动失效是第二重保险。

**开启 / 关闭 / 观测**(以 namespace `approvo`、deployment `approvo` 为例):

```bash
# 开启(填当月)
kubectl -n approvo set env deployment/approvo APPROVAL_BYPASS=2026-08

# 提前手动关闭(否则下月 1 号 0 点自动失效)
kubectl -n approvo set env deployment/approvo APPROVAL_BYPASS-

# 观测:启动日志会公示当前状态(已生效/未生效 + 何时自动失效)
kubectl -n approvo logs deploy/approvo | grep 审批直通
```

启动日志形如:
`[startup] ⚠️ 审批直通已生效(APPROVAL_BYPASS=2026-08):QA 秒通过 / 生产只留痕不部署;到下月 1 号 0 点(CST)自动失效`

⚠️ 用 `kubectl set env` 打的环境变量**不在 chart 里**,下一次 `helm upgrade` 会把它冲掉
(对这个开关而言,"被冲掉"= 回到需审批,是安全的那一侧)。

## config.yaml

```yaml
default_project: "MyProject"                # (可选) 卡片标题前缀,如 "MyProject · 发版申请 · 待审批"
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

# ── 三条"能碰集群"的通道,各自都有独立白名单;不配即整条通道关闭(fail-close) ──
# 判据统一:cluster / namespace 都是【外部可填】的,绝不能因为"调用方说要 ns X"就去操作 ns X。
# 同一个集群里往往还跑着别的团队的生产 namespace —— 靠"我们在那儿没有 RBAC"兜底不是设计,
# RBAC 是随时会被别的需求放开的运维状态,白名单才是写在配置里的显式意图。

viewer_sa_default: ""                       # (可选) 只读 SA 的全局兜底名;留空=必须逐目标写 sa
viewer_targets:                             # 只读凭证(/viewer-token + 面板按钮)
  - label: "🔍 生产只读 30 分钟"
    cluster: "<cluster_name>"
    namespace: "<k8s_ns>"
    sa: "<readonly_sa>"                     # 🔴 必写(或配 viewer_sa_default):approvo 不猜账号名
    minutes: 30
    prod: true                              # 生产按钮渲染成红色

secret_release_key: my-secret               # secret 变更默认挂到哪条 releases 条目
secret_targets:                             # Secret 变更(/secret/request → 审批 → /secret/commit)
  - cluster: "<cluster_name>"
    namespace: "<k8s_ns>"
    release_key: my-secret                  # (可选) 让不同项目的变更走各自的 release 条目

deploy_targets:                             # 动态部署凭据(/deploy-credential)
  - cluster: "<cluster_name>"
    namespace: "<k8s_ns>"
    sa: "<narrow_deployer_sa>"              # 🔴 必写:同样不猜

menu_targets:                               # (可选) 机器人菜单 event_key → 动作白名单
  viewer_prod_app:                          # 不配则用 app/events.py 的内置示例表
    kind: viewer                            # viewer / not_ready / release_key
    cluster: "<cluster_name>"
    namespace: "<k8s_ns>"
    label: "示例应用 生产"

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
      env: prod                             # 🔴 必填:解析不出 env 就【拒绝派发】,绝不默认 prod
      inputs:                               # 可选:追加 workflow inputs
        foo: bar
    # 真实部署目标:显示在审批卡片上,并决定往哪个 cluster/namespace 签发部署凭据。
    # 🔴 必须显式配 —— approvo 不按项目名/环境名猜 namespace。配漏了就不发券、
    #    流水线明确失败,而不是"某些应用恰好能用"(那种兜底会掩盖配置根本没接上)。
    deploy:
      cluster: <cluster_name>               # 需与 deploy_targets 里登记的组合一致
      namespace: <k8s_ns>
    # (可选) 让审批卡片显示【数据库迁移变更】。不配则整块不渲染。
    db_migrations:
      repo: <repo_holding_migrations>       # 迁移文件所在仓(通常是部署仓)
      path: db/migrations                   # 目录前缀,默认 db/migrations
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

## 数据库迁移变更如何显示在卡片上

迁移文件通常不在业务仓,而在**部署仓** —— 于是"业务仓 compare"天然看不见它:
一次带着 `DROP COLUMN` 的发版,卡片上仍然只写"仅镜像变更"。审批人以为自己批的是换镜像,
实际发生的是改生产库结构。

配了 `db_migrations` 之后,卡片会出现「🗄 数据库变更」区块,状态严格分三种:

| 状态 | 卡片显示 |
| --- | --- |
| 有变更 | 列出文件;命中破坏性语句(DROP / 改列类型 / SET NOT NULL / RENAME / TRUNCATE / DELETE)加 🔴 红标,并说明**镜像可回滚、schema 不可回滚** |
| 无变更 | "自上次成功部署以来迁移目录没有变更" —— 并说明基线是什么 |
| **查不到** | "⚠️ 无法判定(原因)" —— **绝不显示成"无变更"**:把无知包装成安全会让审批人放松审查 |

⚠️ **口径**:基线是"本应用上次成功部署的**时间**",不是"数据库里未应用的迁移"
—— approvo 够不到数据库。卡片里如实写明这一点;真正会执行哪些,以你的迁移门禁为准。

## 三条通道共用的一条判据:不猜

`viewer_targets[].sa`、`deploy_targets[].sa`、`releases.*.deploy`、`github.env` ——
这些字段**漏配时都是拒绝执行,而不是取一个默认值**。原因不是洁癖:

- 曾经 `github.env` 默认 `"prod"`:任何一个 stage 漏写 env,就会**静默派发到生产**。
- 曾经按项目名猜 namespace:让"配置根本没接上"这件事在一部分应用上看不出来
  (它们恰好命中兜底),而在另一部分应用上突然炸 —— **一条路能走通,掩盖了另一条路没接上**。
- 曾经请求模型里写 `cluster: str = "<某个生产集群>"`:少传一个字段就打到生产。

⇒ 缺省值指向生产是这类设计最常见的致命错误。配漏了要**大声失败**,不要"也许这是设计"。

## 常见配置陷阱

- **`replicaCount` != 1**:chart 会 fail;SQLite 单副本硬约束
- **`GITHUB_APP_OWNER` 未设**:approvo 启动崩 KeyError (**这是设计**,防误接前公司组织)
- **`ADMIN_PASSWORD` 未设**:`/admin` 整体禁用,GitHub 用户映射只能通过首次启动灌 `user_map` 或直接改 SQLite
- **`container` 字段错**:kubectl set image 会报 `container xxx not found`;chart 生成的 pod 里容器名可能带前缀
- **`github.ref` 拼错**:workflow_dispatch 会 422;approvo 在日志里能看到
- **`source_repo` 不写**:变更清单区块完全不渲染 (不报错)
- **`viewer_targets` / `secret_targets` / `deploy_targets` 不写**:对应通道**整体关闭**(403 / 不发券),
  这是刻意的 fail-close;升级到本版本时请**先更新 config 再更新镜像**,别把正常路径打断
- **`releases.*.deploy` 漏配**:审批卡片会显示"⚠️ 集群缺失",且**不签发部署凭据** ——
  强制动态凭据的流水线会在"取凭据"这步明确失败(而不是悄悄回落到 runner 上的长期 kubeconfig)
- **新增端点忘了改 Ingress**:公网调用得到的 404 与"路径不存在"完全一样、无法区分。
  判据:对照 `/release`(422=能到应用)与 `/no-such-path`(404),立刻分清是哪一层
- **飞书审批定义控件顺序变了**:approvo 按 `form_field_ids` 里的 id 匹配,和顺序无关;但控件 id 错会导致值绑不上
