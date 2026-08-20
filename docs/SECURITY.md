# Security Notes

**不是 vulnerability disclosure policy**(那在项目根的 [`SECURITY.md`](../SECURITY.md)),本文说明 approvo 的**架构级安全边界**——你在设计部署形态时需要知道的信息.

## 威胁模型

| 攻击面 | 谁能碰 | 缓解 |
| --- | --- | --- |
| `/release` HTTP 接口 | 拿到 `RELEASE_TOKEN` 的攻击者 | `secrets.compare_digest` 常时对比;**按端点分桶**限流,认证失败走独立拒绝桶 |
| 匿名 DoS(用错 token 刷接口) | 任何能打到公网入口的人 | 曾经是**一个全局桶** ⇒ 单人可让整个网关瘫掉;现按端点分桶 + 认证失败不吃业务额度 |
| `/admin` 页 | 拿到 `ADMIN_PASSWORD` 的攻击者 | Basic Auth(用户名默认 `admin`=已知)+ **暴力尝试有天花板**;`ADMIN_PASSWORD` 未设即禁用整个 admin |
| 接口自描述信息 | 任何能打到公网入口的人 | `/docs`、`/redoc`、`/openapi.json` **默认关闭**(`ENABLE_DOCS=1` 才开) |
| 改集群 Secret / 签发凭证的目标 | 拿到 `RELEASE_TOKEN` 的攻击者 | `cluster`/`namespace` 是请求体任意可填的 ⇒ 三条通道各有**配置白名单**,不配即整条关闭 |
| 审批直通开关 | 有集群写权限的人 | 只能改 deployment 环境变量来开;**生产 fail-close**,且授权月跨月**自动失效** |
| 飞书审批伪造 | 拿到审批群成员账号的攻击者 | 群成员=审批人;人事流程管拉群 |
| GitHub App token 泄漏 | 拿到 `GITHUB_APP_PRIVATE_KEY` 的攻击者 | token 缩到单仓库 + 只 `actions:write`,50min 自动轮换 |
| kubeconfig 泄漏 | 拿到 approvo 容器 shell 的攻击者 | 受限 SA (只对目标 ns 的 deployment 有 patch + rollout);建议 method=github 完全不放 |
| SQLite 数据篡改 | 拿到 PVC 或 pod shell 的攻击者 | 部署凭证不在 SQLite;篡改 spec_json 影响本次部署,不影响历史部署 |

## 关键设计原则

### 1. 凭证隔离 (recommended: method=github)

approvo 的推荐部署形态是**只调 GitHub `workflow_dispatch`,不持任何集群凭证**:

- approvo 只有:飞书 App Secret + `RELEASE_TOKEN` + `ADMIN_PASSWORD` + GitHub App private key
- 真实部署凭证 (kubeconfig / AWS AK / SSH key) 只在**受限发版仓**的 GitHub Secrets
- 受限发版仓只有少数运维/负责人写权限,业务仓开发无写权限
- 结果:业务仓开发即使拿到自己仓写权限,也无法偷凭证、也无法绕过审批

如果你必须用 `method=kubectl` / `method=helm` (approvo 直连集群),请:
- 用**最小权限 SA**:只 `patch deployment` + `rollout status` on 目标 ns
- 不要给 approvo cluster-admin
- kubeconfig 只挂 read-only

### 2. 公网暴露面

approvo 的推荐 Ingress 配置(见 chart 或 [deploy/k8s.yaml](../deploy/k8s.yaml)):

- **公网 Ingress** — **白名单式**暴露(默认 `/release` + `/healthz`;需要时再加 `/viewer-token`、
  `/release-key`、`/deploy-credential`).**`/admin`、`/api/*` 在这条 Ingress 上没有路由**,
  公网够不到 (结构性隔离,不依赖认证)
- **内网 Ingress** — 全部路径 (含 `/admin`),仅内网 DNS 解析

即使 `RELEASE_TOKEN` 泄漏,攻击者也无法调 `/admin` 修改 user_map.

⚠️ **代价要知道**:新增端点必须**同步加路由**,否则公网调用得到的 404 与"路径不存在"完全一样、
无法区分(实测踩过:某端点在 Pod 内是 401=存在,经网关是 404=没放行,流水线只报"HTTP 404")。
判据:对照 `/release`(422=能到应用)与 `/no-such-path`(404),立刻分清是哪一层。

### 3. 飞书长连接 (出方向)

approvo 不需要飞书事件的公网入站入口:
- lark-oapi SDK 主动建长连接到 `open.feishu.cn:443`(出方向)
- 事件通过长连接推给 approvo
- 出方向能连即可,不用配 webhook + encrypt_key

**如果内网出方向被防火墙限制**:开个白名单 `*.feishu.cn:443` 出方向即可.

### 4. 事件重放 / 事件重复

飞书长连接偶发会重发事件.approvo 用 `try_claim_for_deploy(instance_code)` 做原子 CAS:

```sql
UPDATE releases SET status='deploying' 
  WHERE instance_code=? AND status='pending'
```

`rowcount == 1` 才继续部署;失败(pending 已被别的线程翻掉)即返回.**这保证了每个 instance 只部署一次.**

启动时的对账(`reconcile_pending_once`)也调 `process_instance`,同样受 CAS 保护.

## 已知安全边界

### SQLite 单副本

- 单节点故障 = 断服.**没有 HA**.
- PVC 建议定期快照 (velero / cluster snapshot)
- 未来加 Postgres backend 支持多副本 HA (在 [CHANGELOG.md](../CHANGELOG.md) unreleased 节)

### 长连接故障恢复

- 长连接断开时,飞书**不会重投**期间发生的事件
- approvo **启动时**对账一次 pending 实例 → 能补上"进程离线/重启期间被决策"的那些
- ⚠️ **剩下的缺口**:若在运行中长连接抖动的那几秒里审批被决策,事件会丢,该 release 一直 pending
  (表现为"批了却不部署",**不会误部署**)。恢复手段:重启 approvo(触发对账)。
  彻底修法是在长连接**重连**回调里再对账一次 —— 而不是退回每分钟轮询(那会吃光 IM 的 API 月额度)

### 秘钥轮换

- 飞书 App Secret / GitHub App private key / `RELEASE_TOKEN` / `ADMIN_PASSWORD` **必须定期轮换**(建议每 90 天)
- 轮换步骤:改 secret → `kubectl rollout restart deploy/approvo` → 验证 `/healthz`
- 长连接会自动重连 (新 secret 生效)

### 动态部署凭据 (`/deploy-credential`)

把"长期 kubeconfig 常驻 runner 磁盘"换成"每次部署签一副、用完即毁"。取舍与实测边界:

- **两样都要**:`grant`(一次性、10 分钟、内存态)+ `RELEASE_TOKEN`。
  只有 token 不够 —— 它是共享的,单凭它就能换到部署凭据等于把"能提审批"升级成"能直接部署";
  只有 grant 也不够 —— grant 会作为 workflow 输入落进 run 元数据(GitHub UI 可见)。
- **必须绑定一次性对象**:`TokenRequest` 的 TTL **下限是 10 分钟**(请求 1m/5m 会被 API server 直接拒绝),
  而部署通常 1~2 分钟。删掉绑定对象后实测 **15 秒**内失效(认证缓存约 10 秒)
  ⇒ 暴露窗口 = 部署时长 + ~15 秒,而不是 TTL 的 10 分钟。
- **槽位固定名池 + 槽满即拒**:k8s 的 `create` 无法按资源名限制,但 `delete` 可以 ——
  固定 8 个槽位名,RBAC 里 `delete` 用 `resourceNames` 限死这 8 个,
  approvo 因此**没有**删该 namespace 里其它 Secret(比如数据库口令)的权限。
  槽满时**拒绝签发**,绝不退化成"不绑定就签"。
- **不落库**:grant 只活 10 分钟,落库会让它进数据库并被备份带走 ——
  一个短命凭证不该有比自己更长的副本。approvo 重启即全部失效,这是期望行为。
- ⚠️ **目标必须登记 `deploy_targets`**,否则**不发券**:让流水线明确失败,
  而不是让它悄悄回落到 runner 上的文件(那正是这套机制要消灭的东西)。

### 审批直通模式 (approval bypass)

`APPROVAL_BYPASS`(见 [CONFIGURATION.md](CONFIGURATION.md#审批直通模式-approval-bypass))
是一条**有意保留的、绕过审批门禁**的通道,用于审批平台不可用(如 IM 的 API 额度耗尽)时仍能发版。
把它放进威胁模型是因为它降低了"人工把关"这层保障,其安全性依赖以下三条设计,缺一不可:

- **生产 fail-close**:开启后 `env≠qa`(生产 / env 缺失)只落库置 `failed`、**绝不自动部署** ——
  绕过的只是 QA,生产仍需人工线下执行,不存在"未经审批的版本被自动推上生产"。
- **自带过期**:取值必须是授权月 `YYYY-MM`,跨月(下月 1 号 0 点)自动失效 ——
  即便运维忘记关,门禁最长敞开到当月月底,而非无限期。
- **可归因 + 可观测**:只能改 deployment 环境变量来开(需集群写权限,天然受 RBAC 约束),
  且启动日志公示当前状态;它刻意不落 git,避免"配置漂移成常态"。

⚠️ 开启期间 `/release` 的共享 `RELEASE_TOKEN` 就等价于"QA 直接发版权"(无审批兜底),
因此**只应在需要时短期开启**,用完(或跨月自动失效后)确认已恢复需审批。

### 审批人指定

- 审批人 = 审批群成员 (动态)
- **谁能拉/踢群** = 谁能授/撤审批权
- 建议:拉/踢群本身有对应的审计日志

### 不支持

- **零信任审批**(每次都要多人多因素) — approvo 是**或签** (任一群成员通过即部署);会签需 provider 层面改造
- **审批延迟**(比如审批通过后 24h 才部署) — 目前是"通过即部署",没有延迟触发
- **审批链**(初审 → 复审 → 部署) — 用飞书审批定义的多节点可以做,但 approvo 只监听终态

## 上报漏洞

见 [根 SECURITY.md](../SECURITY.md).
