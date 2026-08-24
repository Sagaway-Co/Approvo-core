# Changelog

All notable changes to approvo are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- 🔴 **长连接线程崩溃后不再静默死亡 —— 「/healthz 绿 ≠ 审批能用」**。`_run_ws` 原实现是裸的 `cli.start()`:`app_id` 无效(或凭证轮换 / 应用被停用 / 网络抖动让 SDK 内部重连耗尽)时,异常在【连接阶段】直接 raise,未捕获异常把整个 WS 线程带走,**且没有任何重试**。而 uvicorn 在主线程毫发无伤 —— 实测 `connect failed=1 / 线程崩溃=1 / 容器 restarts=0 / 容器 ready=true / /healthz={"ok":true}`。
  - 后果是【静默失效】:卡片照样能创建(走 HTTP REST,与 WS 独立)、人照样能在 IM 里点通过,但审批事件永远收不到 → **什么都不会部署,也没有任何告警**。CI 侧的 fail-close 只看 `POST /release` 是否 2xx,而那时 HTTP 是好的。
  - 注:原注释「阻塞 + 自动断线重连」本身没错,但重连是 SDK 在 `start()` **内部**的行为,前提是 `start()` 已成功进入阻塞;连接阶段就 raise、或 SDK 放弃重连并上抛,异常都会穿透到线程顶层。
  - 修法:`_run_ws` 改为**监督循环**,任何异常都不许结束线程,退避重启(2s 起、指数、上限 300s)。`start()` 正常情况永不返回,故**返回本身也当作崩溃处理**——否则会静默退出循环,等于把同一个 bug 换个形式再犯。曾稳定运行 ≥60s 则退避归零,避免"跑了半天偶发抖动"被迫等满 300s。
  - 退避是刻意的:凭证失效时 `start()` 会立刻 raise,不退避就会疯狂重连把 API 月额度打爆 —— 而额度耗尽本身又会让 IM 全线不可用。

### Added

- **`app/wshealth.py`:把「审批链路是否真的可用」变成可判据的东西**。`cli.start()` 是阻塞调用、SDK 不给"已连上"回调,故取「`start()` 正在阻塞中且未抛异常」为最强信号,并额外要求**已持续 `GRACE_SEC`(15s)** —— 无效凭证在几百毫秒内就 raise,撑不过这个门槛。fail-close:`init` / `starting` / `crashed` 一律判不健康。状态只放内存(它描述本进程当前连接,落库反而会让上个进程的状态污染新进程)。
- **`GET /readyz`(能力探针)**:长连接不可用 → 503。`GET /healthz` 语义收敛为 **liveness,恒 200**,但 body 带 `ws` 真实状态(不再是硬编码 `{"ok":true}`)。
  - ⚖️ 为什么 `/healthz` 不许变红:容器编排的 readinessProbe 通常指着它,红了实例会被摘出服务发现 → 连 `/admin` 和日志入口都够不到、现场无法诊断,而 CI 只会看到"连不上 gate"这种无信息量的错误。**外部监控/拨测指 `/readyz`,不要把 readinessProbe 改过来。**
  - 直通模式下 `/readyz` 返回 `ok=true`(那条路不依赖长连接),但仍如实报 `ws.healthy=false` —— 否则"开着 bypass 时 WS 挂了"会被永久掩盖。
- **`POST /release` 增加审批链路 fail-close**:长连接不健康时返回 **503** 拒绝受理。此时建审批实例只会**静默积压**(CI 拿到 2xx 以为进了流程、卡片正常出现、人点完通过就走了,没有任何一环报错)。拒绝发生在建实例【之前】,不留半个实例。**直通模式例外**——检查放在 bypass 分支之后,否则 IM 出问题时连应急通道也一起堵死。

### Changed

- **启动对账加单次上限(`RECONCILE_MAX=50`)+ 逐条节流(0.2s)**。对账是【突发】调用:所有 pending 在启动瞬间集中核对,每条至少一次 `get_instance()`;原实现无上限,积压上百条时一次重启就是上百次突发调用,而月额度正是被高频调用吃光的。
  - 🔴 被跳过的条数**显式告警**(`[reconcile][warn]`)。只加上限而不喊出来,等于把"突发打爆额度"换成"静默不对账"——同一类错误(判据落在不反映真实情况的指标上)的又一次复发。

### ⚠️ Breaking

- **三条"能碰集群"的通道现在都需要显式配置目标白名单,不配即整条通道关闭(fail-close)**:
  - `secret_targets` — 不配则 `/secret/request` 一律 403
  - `deploy_targets` — 不配则不签发部署凭据、且**不发兑换券**
  - `viewer_targets[].sa`(或顶层 `viewer_sa_default`)— 不配则拒绝签发只读凭证,不再回落到某个内置 SA 名
  ⇒ **升级顺序:先更新 config,再更新镜像**,否则会把正常路径打断。
- **请求模型不再有指向生产的默认值**:`/viewer-token` 与 `/secret/request` 的
  `cluster`/`namespace` 变为必填,缺字段直接 422。
- **`github.env` 不再默认 `prod`**:解析不出 `env` 就拒绝派发。
- **`/docs`、`/redoc`、`/openapi.json` 默认关闭**,需 `ENABLE_DOCS=1` 才开。

### Added

- **动态部署凭据 `/deploy-credential`**(+ `/deploy-credential/revoke`):取代"长期 kubeconfig
  常驻 runner 磁盘"。approvo 派发部署时签一张一次性兑换券(内存态、10 分钟),流水线用
  `券 + RELEASE_TOKEN` 换一枚 10 分钟、限定 namespace、**绑定一次性对象**的 SA token,
  部署结束回调撤销。实测边界:`TokenRequest` TTL 下限 10 分钟(1m/5m 会被 API server 拒绝);
  删绑定对象后 15 秒内失效 ⇒ 暴露窗口 = 部署时长 + ~15 秒。槽位固定 8 个名字
  (RBAC 用 `resourceNames` 限死),槽满**拒绝签发**,绝不退化成"不绑定就签"。
- **审批卡片显示数据库变更**(配置驱动:`releases.<key>.db_migrations`)。迁移文件在部署仓,
  业务仓的 compare 天然看不见 —— 此前一次带 `DROP COLUMN` 的发版,卡片上仍写"仅镜像变更"。
  三态严格区分:`ok`(列文件 + 破坏性语句红标 + 回滚陷阱说明)/ `none`(说明基线)/
  **`unknown`(无法判定)** —— 查不到绝不渲染成"无变更"。没配这段的应用整块不渲染。
- **审批直通模式 `APPROVAL_BYPASS`**(审批平台不可用时的受支持发版通道,**月度自动失效**):
  值为授权月 `YYYY-MM`;`env=qa` 跳过审批直接走原有部署流程("秒通过"),
  `env≠qa`(生产 / env 缺失)**只落库置 `failed`、不部署**,由人工线下执行。
  跨月即自动回到"需审批",不依赖任何人记得去删变量。见 docs/CONFIGURATION.md + docs/SECURITY.md。
- **`POST /release` 新增 `git_tag`**(可选,缺省回落 `tag`):镜像 tag 与真实 git tag 可能不同
  (一仓两制的仓库,CI 会剥掉前缀),promote 校验改用 `git_tag or tag` ——
  由制造特例的一方自报真实 tag,平台不为单仓背 `tag_prefix` 配置。
- **`releases.<key>.deploy`**:真实部署目标(cluster/namespace)。会显示在审批卡片上
  (改前 GitHub 方式是把 `owner/repo` 塞进"集群"位,真实目标完全隐形),
  并决定往哪里签发部署凭据。两侧都空时卡片显示"⚠️ 集群缺失",让配置缺失**大声可见**。
- **`menu_targets`**:机器人菜单目标改为配置驱动(不配则用内置示例表)——
  开通一个新环境不必改代码。
- **`secret_targets[].release_key`**:不同项目的 Secret 变更可挂到各自的 `releases` 条目,
  审批卡片因此能看出"这是谁的变更";未指定时回落 `secret_release_key`。
- 回归守卫:`tests/test_security_hardening.py`、`test_deploy_credential.py`、
  `test_secret_targets.py`、`test_db_change_visibility.py`、`test_approval_bypass.py`、
  `test_release_prereq_errors.py`、`test_failclose_guards.py`,以及 `test_store_pg.py` 里
  三条**打真库**的 stage 感知用例。

### Changed

- 🔴 **移除每 60s 的常驻对账轮询,改为「启动时对账一次」**(`poller_loop` → `reconcile_pending_once`)。
  原实现对**每一条 pending** 每 60s 查一次审批状态,而审批"等人点通过"往往要几小时 ——
  一条审批过一夜≈近千次 API 调用,而这些调用**什么也没发现**;实测这一项几乎吃光了 IM 租户的
  基础 API 月额度。实时性本来就由长连接推送保证。现在成本 = 启动瞬间的 pending 条数(通常为 0)。
  ⚠️ 残留风险(已写进代码注释与文档):运行中长连接抖动那几秒里被决策的审批可能丢事件,
  该 release 会一直 pending 且不部署(**不会误部署**),靠下次重启时的对账补上。
- **限流按端点独立分桶**,并把认证失败挪进**独立的拒绝桶**:
  原实现是一个全局桶且**先计数后校验 token**,于是任何人用错 token 刷 `/release`
  就能让 `/viewer-token`、`/secret/*`、`/release-key` 对所有人不可用。
- **`/admin` 的 Basic Auth 补上暴力尝试上限**(用户名默认 `admin` 是已知的,
  且 `/admin` 可能在公网 host 上可达 —— 关掉 admin ingress 并不会关掉这条路径)。
- **`/secret/commit` 补上限流**(它恰恰是直接写集群的那一步,原先完全没有)。
- **公网 Ingress 的放行路径改为白名单式可配**(helm `ingress.public.paths`)。
  ⚠️ 新增端点必须同步加路由,否则公网调用得到的 404 与"路径不存在"无法区分。

### Fixed

- 🔴 **判重 / 幂等 uuid / 两处"上次成功"基线全部改为 stage 感知**(同一根因的四处面貌):
  1. `status_history` 不带 stage ⇒ 同一个 tag 发到第二个环境被判成 `already deployed`,
     返回 **HTTP 200 而实际根本没执行**("静默跳过"比报错危险得多);
  2. 幂等 uuid 不带 stage ⇒ 修好上一条后两侧 `len(history)` 都变 0,uuid 反而更容易完全相同
     → IM 侧 uuid conflict → 500;
  3. `last_success_commit` 不带 stage ⇒ 卡片谎报"与已部署版本相比无新增提交";
  4. `last_success_at` 不带 stage ⇒ 卡片谎报"迁移目录没有变更",而那次真的新增并应用了迁移。
  ⇒ 3、4 是**谎报安全**:审批人看到"无变更"就会放松审查。修这类问题必须一次修完同族所有键。
- **`/commits/{ref}` 对不存在的 ref 返回 422 而不是 404**:只处理 404 会让 422 冒泡成
  HTTPError → 被 `/release` 的宽捕获包成 502「check-prereq 无法校验」,
  而它本该是一句指向明确的 409「tag 不存在」。
- **读不到 App 安装信息(404)时报 409 + 三条可照做的排查指引**,不再是裸 502。
  新增 `github.AppInstallLookupError` 把"配置错误"与"校验服务不可用"分开 ——
  ⚠️ 404 **不等于**"App 没装":`GITHUB_APP_ID` 与私钥不属于同一个 App 时也会 404。
- **`spec` 必须显式复制 `deploy` 字段**:`spec` 是白名单式逐字段组装的,
  "config 里加了字段"≠"派发那一步看得见"。
- **移除"按项目名猜 namespace"的兜底**:猜出来的目标让"配置漏了"在一部分应用上看不出来
  (它们恰好命中兜底)、在另一部分应用上突然炸 —— 一条路能走通,掩盖了另一条路根本没接上。
- **`/secret/commit` 不再把 `cluster` 兜底成某个默认集群**:授权记录里没带 cluster 时
  原实现会**默认往那个集群写 Secret**;现改为 fail-close(409,要求重新发起申请)。
- **声明了 `stages` 却送来未登记的 stage → 400 拒绝**,不再回落顶层配置
  (拼错 stage 名叠加旧的 `env` 默认值 = 静默打到生产)。
- **`viewer_sa_for` 改为 fail-close**:未登记的组合、或登记了却没写 `sa` 且无全局兜底,
  一律抛错;`issue_viewer_kubeconfig` 把该异常转成 `(False, 说明)`,不让它变成 500。

### Added
- **申请人两行显示**:submit_card "申请人" 字段第一行真名 + 第二行 `` `github_actor` ``(灰色小字), 让审批人核对是谁在推 tag.未映射时显示 `` `<actor>` `` + "(未映射)".新 `user_map` 扩展格式支持 `{user_id, name}` dict, 老 `login: "<user_id>"` 字符串格式向后兼容
- **卡片标题项目名前缀**:多项目共用一个 approvo/审批群时用来分辨.顶层 `default_project` + release 级 `project` 字段 + `POST /release` body `project` 字段三级覆盖(后者高优).所有卡片(申请/部署中/成功/失败/拒绝)标题前面自动加 "<项目名> · " 前缀
- **Dockerfile 支持内网 build**:新 `PYTHON_BASE` build-arg (默认 `python`,内网可传 `--build-arg PYTHON_BASE=<内网 mirror>/library/python` 完全脱离 docker.io)
- **Stage-based approval routing**:`POST /release` 加 `stage` 字段(pre/release/hotfix);`feishu.approval_codes` 配置支持不同 stage 分派不同审批定义(比如 pre=群成员或签, release=CEO 固定).向后兼容 `approval_code` 单值
- **生产/紧急发版视觉强化**:`stage in (release, hotfix)` 或 `env=prod` 时,申请/部署中/成功/失败/拒绝所有卡片自动加 ⚠️ 装饰围栏(顶+底各 1 行),申请卡 header 切 red,提示语强化
- **Lark (海外版) 支持**:新 env `LARK_ENDPOINT`,取值 `feishu` (默认) / `lark` / 完整 URL;SDK 长连接 + REST API BASE 一起切换.helm chart values 里在 `secrets.LARK_ENDPOINT` 暴露

### Planned
- Provider abstraction 深化:让 wechat / dingtalk / slack provider 各自完整实现
- Postgres store backend(替代 SQLite,支持多副本 HA)
- 审批卡片"查看部署日志"按钮(链到 GitHub Actions run)
- 失败自动回滚 (`kubectl rollout undo`)

## [0.1.0-alpha] - 2026-07-10

首个产品化版本。从内部工具重构为可开源产品雏形。

### Added
- **Abstraction layers**:`ApprovalProvider` / `Deployer` / `ChangelogSource` ABC + factory
- **Helm chart** (`deploy/helm/approvo/`):values + templates 完整;硬约束 SQLite 单副本
- **Multi-stage Dockerfile**:builder + runtime,~180 MB with kubectl + helm
- **CI/CD workflows**(GitHub Actions):
  - `ci.yml` — ruff + mypy + pytest + `grep-<legacy-identifier>` provenance gate
  - `docker.yml` — build + push GHCR (multi-tag semver + branch + sha)
  - `helm.yml` — helm lint + package
  - `release.yml` — GitHub Release from CHANGELOG section
- **Examples**:
  - `examples/caller-workflows/build-and-request-approval.yml` — 业务仓侧 (build + curl /release, fail-close)
  - `examples/restricted-deploy-repo/deploy.yml.template` — 受限仓部署 workflow 模板
- **Docs**:README + GETTING_STARTED / ARCHITECTURE / PROVIDERS / DEPLOYERS / CONFIGURATION / SECURITY
- LICENSE (Apache-2.0) + CONTRIBUTING + SECURITY policy

### Changed
- 产品重命名 `feishu-release-gate` → `approvo`
- `config.example.yaml` 全体重写为通用最小骨架 (4 个 method 示例)
- `deploy/k8s.yaml` ConfigMap 缩到 demo 单 release,Ingress 域名占位符
- `app/github.py`:`GITHUB_APP_OWNER` fallback 移除,缺失即 KeyError

### Removed
- 前公司相关的示例 workflow (`workbench-workflows/`, `sagaway-app-workflows/`, `release-deploy-repo/`)
- 前公司内部交接文档 `HANDOFF.md`
- 错误位置的 `.github/workflows/deploy.yml`(该文件属于受限发版仓,不属于本仓)

### Security
- 所有前公司标识 (<legacy-identifier> 品牌、组织、应用名、ACR 主机、内网 IP) 一次性清除
- CI `grep-<legacy-identifier>` gate 防未来回退

[Unreleased]: https://github.com/Sagaway-Co/Approvo-core/compare/v0.1.0-alpha...HEAD
[0.1.0-alpha]: https://github.com/Sagaway-Co/Approvo-core/releases/tag/v0.1.0-alpha
