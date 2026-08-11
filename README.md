# approvo

> Release approval gate for CI/CD pipelines · powered by Feishu (国内) / Lark (海外).
> 打 tag → 审批 → 通过后自动部署.一个轻量、单副本、可开源的发版卡点服务.

[![CI](https://github.com/Sagaway-Co/Approvo-core/actions/workflows/ci.yml/badge.svg)](https://github.com/Sagaway-Co/Approvo-core/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

---

## 为什么用 approvo

发版是团队里少数几个必须"人工把关 + 自动执行"的动作.现有的选择要么太重 (Spinnaker / ArgoCD Sync Policies),要么依赖公司内 IM 平台且事件链路脆弱 (企微 webhook + 定时 poll).

approvo 的取舍:
- **飞书长连接** — 无需公网入站入口,出方向能到 `open.feishu.cn:443` 即可
- **动态审批人** — 审批人 = 审批群当前成员;拉/踢群 = 即时授权/撤销,不用改配置
- **原子状态机** — SQLite `try_claim_for_deploy` CAS 防重复部署,60s 兜底轮询补漏事件
- **凭证隔离** — approvo 只调 GitHub `workflow_dispatch`,不持 kubeconfig;真实凭证只在受限发版仓
- **变更清单** — 审批卡片自动列出 PR + 直接提交 + 作者,让 review 有事实基础
- **可替换** — provider (飞书/钉钉/Slack) + deployer (github/kubectl/helm) 抽象层就位

## 架构一览

```
业务仓 (开发可写)
   └─ push tag / workflow_dispatch
        └─ .github/workflows/release.yml
             └─ curl POST /release  ────► approvo
                                             │ 落库 + 拉群成员 + 建飞书审批 + 发申请卡
                                             ▼
                                        (审批群任一成员点通过)
                                             │ 长连接事件 (或 60s 轮询兜底)
                                             ▼
                                        try_claim_for_deploy (原子 CAS)
                                             │
                                             ├──► method=github  ──► workflow_dispatch → 受限发版仓 deploy.yml
                                             │                       (kubeconfig / cloud AK 只在此仓)
                                             ├──► method=kubectl ──► kubectl set image + rollout
                                             ├──► method=helm    ──► helm upgrade --reuse-values
                                             └──► method=dryrun  ──► 不连集群,链路联调用
                                             ▼
                                        部署结果卡 → 结果群
```

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 3 分钟跑通 dryrun

```bash
git clone https://github.com/Sagaway-Co/Approvo-core && cd Approvo-core
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml           # 改 approval_code / chat_id
export FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx
export RELEASE_TOKEN=$(openssl rand -hex 16)
python -m app.main

# 另一个终端模拟 CI 触发
curl -X POST localhost:8700/release \
  -H "X-Release-Token: $RELEASE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"repo":"demo","tag":"v1.2.3","github_actor":"alice"}'
```

flush 后你会看到审批群里出现"🚀 发版申请·待审批"卡片,通过后触发 dryrun 部署,结果卡回到结果群.

完整上手 (含飞书应用申请、审批定义、helm 部署) 见 [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).

## 镜像

```bash
docker pull docker.io/sagawayco/approvo-core:main
```

tag 规则：`main`（主分支最新）· `sha-<短 sha>` · 推 `v*` tag 时另有 `1.2.3` / `1.2`。
也可自行构建：`docker build -f deploy/Dockerfile -t approvo:local .`

## 生产部署 (Helm)

```bash
helm install approvo ./deploy/helm/approvo \
  --namespace approvo --create-namespace \
  --set-file config=./config.yaml \
  --set existingSecret=approvo-secrets
```

Chart 提供 configmap / secret / pvc / deployment / service / ingress (public + admin);
`replicaCount != 1` 会被 chart fail 拒绝渲染 (SQLite backend 决定).
详见 [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

### 环境特化 values (⚠️ 关键 pattern)

不同集群 (QA / prod / ...) 的 ingress host、releases 清单等，建议各自放一个 values 文件
(例如 `deploy/environments/<env>.yaml`，**本仓不附带任何真实环境的 values**).更新走这**唯一**一条命令:

```bash
helm upgrade approvo ./deploy/helm/approvo \
  --namespace approvo --reuse-values \
  -f deploy/environments/<env>.yaml
```

**必须同时用 `--reuse-values` 和 `-f <env>.yaml`**:

- `--reuse-values` 保留 helm history 已有值 (feishu.user_map / approval_codes 等敏感或长期演化的字段)
- `-f qa.yaml` 叠加环境特化 (ingress + releases + stages),同 key 时后者胜

**反 pattern** (踩过 2 次,冲掉 my-app releases + approvo.example.com ingress):

- ❌ `helm upgrade` 不带 `-f` → chart 默认 `ingress.enabled: false` + 空 releases
- ❌ `kubectl apply` / `kubectl patch secret` 直接改 approvo K8s 状态 → 状态漂移出 Git,下次 helm upgrade 又被冲

任何环境改动 (加 sc-* 新应用 / 换 approval_code / 换 ingress 主机等) 都改 `deploy/environments/<env>.yaml` → 走 PR → merge → 跑上面 helm upgrade.

## 业务仓接入

在业务仓放 [examples/caller-workflows/build-and-request-approval.yml](examples/caller-workflows/build-and-request-approval.yml),
配 `RELEASE_GATE_URL` + `RELEASE_GATE_TOKEN` 两个 secret 即可.构建后 curl approvo `/release`,fail-close(approvo 不可达即 workflow fail).

对于凭证隔离更严的场景,建议同时建一个受限发版仓,放 [examples/restricted-deploy-repo/deploy.yml.template](examples/restricted-deploy-repo/deploy.yml.template).

## 目录结构

```
app/
  main.py / server.py / events.py / settings.py / store.py       核心
  forms.py / cards.py                                            审批表单 + 飞书卡片
  providers/{feishu,__init__}.py                                 ApprovalProvider ABC + 实现
  deployers/{github,kubectl,helm,dryrun,__init__}.py             Deployer ABC + 实现
  sources/{github,__init__}.py                                   ChangelogSource ABC + 实现
  feishu.py / github.py / deployer.py                            (legacy modules,被 abstraction 层 wrap)
deploy/
  Dockerfile / .dockerignore                                     多阶段构建
  k8s.yaml                                                       all-in-one 部署清单 (无需 helm)
  helm/approvo/                                                  Helm chart
examples/
  caller-workflows/                                              业务仓侧调用模板
  restricted-deploy-repo/                                        受限发版仓部署模板
docs/                                                            架构、配置、provider、deployer、security 文档
scripts/
  create_approval.py / get_user_id.py / list_chats.py            飞书辅助脚本
  preview_cards.py / test_deploy.py                              本地调试
```

## 文档索引
- [接入指导手册](docs/INTEGRATION.md) — **把你自己的仓库接上来**，含每一步的判据与真实踩坑

- [Getting Started](docs/GETTING_STARTED.md) — 从零到跑通
- [Architecture](docs/ARCHITECTURE.md) — 长连接 / CAS / dispatch 编排
- [Configuration](docs/CONFIGURATION.md) — config.yaml 全字段
- [Providers](docs/PROVIDERS.md) — 新增审批平台适配器
- [Deployers](docs/DEPLOYERS.md) — 新增部署执行器
- [Security](docs/SECURITY.md) — 威胁模型与已知边界
- [Contributing](CONTRIBUTING.md) — PR / Issue 规范
- [Changelog](CHANGELOG.md) — 版本记录

## 项目状态

`v0.1.0-alpha` — 首个产品化版本.API 与配置字段可能在 v0.x 期间小改;进入 v1 后遵循 SemVer 稳定承诺.

## License

[Apache-2.0](LICENSE).Copyright 2026 Sagaway.
