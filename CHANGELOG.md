# Changelog

All notable changes to approvo are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
