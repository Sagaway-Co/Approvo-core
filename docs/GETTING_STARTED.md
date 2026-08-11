# Getting Started

从零到跑通完整发版链路.预计 30-60 分钟.

## 一、准备飞书 / Lark 自建应用 (一次性)

approvo 同时支持**飞书 (国内)** 和 **Lark (海外)**,两者租户完全隔离,选一个:

| 版本 | 开发者后台 | API endpoint | 需要设置 |
| --- | --- | --- | --- |
| 飞书 | https://open.feishu.cn/app | `open.feishu.cn` | (默认,无需设置) |
| Lark | https://open.larksuite.com/app | `open.larksuite.com` | `LARK_ENDPOINT=lark` |

### 1. 建应用

- 打开对应开放平台,点"创建应用" → "自建应用" (Lark: "Create Custom App")
- 记下 **App ID** (`cli_xxxxxxxxxxxx`) 和 **App Secret** (在"凭证与基础信息" / "Credentials & Basic Info"页,需管理员生成)

### 2. 开权限 (scopes)

在应用 → "权限管理"打开:
- `approval:approval` 或 `approval:instance` — 创建 / 查询审批实例
- `im:message` — 发结果卡到群 (机器人还须被拉入结果群)
- `contact:user.id:read` — 邮箱反查 user_id (不用邮箱可跳)

### 3. 订阅事件 → 选"长连接"

在应用 → "事件与回调" → 选**长连接**方式 (不是 webhook):
- 添加事件 `approval_instance` (审批实例状态变更)

**长连接的好处:不需要公网入口、不需要 encrypt_key.** 出方向能连 `open.feishu.cn:443` 即可.

### 4. 发布应用

"版本管理" → 创建版本 → 提交发布.让权限 + 事件订阅在租户内生效.

## 二、建审批定义,拿 approval_code

先查你和其他审批人的 user_id:
```bash
export FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx
python -m scripts.get_user_id user1@example.com user2@example.com
```

**方式 A (推荐,控件 id 干净):** 用脚本建审批定义

```bash
python -m scripts.create_approval --approvers <uid1>,<uid2> --name "发版审批"
# 输出 approval_code=XXXX-XXXX-XXXX,填进 config.yaml
```

**方式 B (脚本报 schema 错时):** 在飞书管理后台 → "审批" 可视化建定义
- 加 8 个文本控件:repo / tag / cluster / namespace / image / operator / commit / notes
- 审批节点选**自选 / Free**(不写死审批人)
- 发布
- 在审批详情页拿 approval_code
- 每个控件的 GUID 填进 `config.yaml` 的 `form_field_ids`

### 建审批群 + 结果群

- **审批群**:审批人来源.approvo 每次发版时实时拉群成员做审批人,拉/踢群 = 授权/撤销
- **结果群**:接收部署中/成功/失败/拒绝卡

两个群都要把 approvo 机器人拉进去.拿到两个 `chat_id` (在群设置 → 群链接的 URL 里,或用 `python -m scripts.list_chats`).

## 三、配置 + 本地试跑

```bash
git clone https://github.com/Sagaway-Co/Approvo-core && cd approvo
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# 改 feishu.approval_code / detail_chat_id / result_chat_id / default_initiator_user_id
# 改 releases 里加你自己的应用 (先用 method=dryrun 联调)

export FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx
export RELEASE_TOKEN=$(openssl rand -hex 16)
export ADMIN_PASSWORD=$(openssl rand -hex 16)   # 启用 /admin 用户映射管理页
python -m app.main
```

模拟 CI 触发:
```bash
curl -X POST localhost:8700/release \
  -H "X-Release-Token: $RELEASE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"repo":"demo","tag":"v1.2.3","github_actor":"alice"}'
```

审批群里出现"🚀 发版申请·待审批"卡片;在飞书审批点通过,approvo 就会执行部署 (dryrun 是假部署) 并往结果群发结果卡.

管理 GitHub→飞书用户映射:浏览器打开 `http://localhost:8700/admin` (Basic Auth,`admin` / `$ADMIN_PASSWORD`).

## 四、部署到 Kubernetes

### 方式 A: Helm (推荐)

> 镜像 `docker.io/sagawayco/approvo-core:main`，chart 默认已指向它；也可自行构建后 `--set image.repository=<your-registry>/approvo`。

```bash
# 建 secret (需要真实凭证)
kubectl create namespace approvo
kubectl -n approvo create secret generic approvo-secrets \
  --from-literal=FEISHU_APP_ID=cli_xxx \
  --from-literal=FEISHU_APP_SECRET=xxx \
  --from-literal=RELEASE_TOKEN=$(openssl rand -hex 16) \
  --from-literal=ADMIN_PASSWORD=$(openssl rand -hex 16) \
  --from-literal=GITHUB_APP_ID=... --from-literal=GITHUB_APP_OWNER=your-org \
  --from-file=GITHUB_APP_PRIVATE_KEY=./app-private-key.pem

# 装 chart (config.yaml 通过 --set-file 传)
helm install approvo ./deploy/helm/approvo \
  --namespace approvo \
  --set-file config=./config.yaml \
  --set existingSecret=approvo-secrets \
  --set ingress.public.host=release.example.com \
  --set ingress.admin.host=release-admin.example.internal

# 观察
kubectl -n approvo get pods,pvc,svc,ingress
kubectl -n approvo logs -f deploy/approvo
curl https://release.example.com/healthz
```

### 方式 B: 直接 kubectl apply (无 helm)

```bash
kubectl apply -f deploy/k8s.yaml     # 会自动创建 approvo namespace
# 手工创建 secret + configmap (deploy/k8s.yaml 里的 configmap 是 demo 版,请自定义)
```

## 五、业务仓接入

把 [examples/caller-workflows/build-and-request-approval.yml](../examples/caller-workflows/build-and-request-approval.yml) 复制到业务仓 `.github/workflows/`.

在业务仓配 2 个 secret:
- `RELEASE_GATE_URL` — approvo 服务地址 (走内网即写内网地址,配 self-hosted runner;走公网就写 `https://release.example.com`)
- `RELEASE_GATE_TOKEN` — 与 approvo 环境变量 `RELEASE_TOKEN` 一致

打 tag 触发:
```bash
git tag v1.2.3 && git push origin v1.2.3
```

链路:业务仓 CI 构建镜像 → curl POST approvo `/release` (fail-close) → 审批群卡片 → 通过 → deploy.

## 六、常见问题

**Q: 飞书群里没收到卡片?**
- 检查机器人是否在群里 (每个群单独拉)
- 检查 `chat_id` 是否正确 (`python -m scripts.list_chats` 列表)
- 检查权限 scopes 是否发布上线

**Q: 长连接一直重连?**
- 出方向必须能到 `open.feishu.cn:443`;检查防火墙 / 代理
- app_id/app_secret 是否正确;permission 发布状态

**Q: workflow_dispatch 报 404?**
- GitHub App 是否安装到目标 org / repo?
- 权限包含 `actions:write` for 受限发版仓?
- `GITHUB_APP_OWNER` 是否正确? (`GITHUB_APP_REPO` 已废弃, 现按 `config.releases.*.github.repo` 自动选)

更多故障排查见 [ARCHITECTURE.md](ARCHITECTURE.md) 的"关键组件"节.


---

服务跑起来之后，接入你自己的业务仓请看 **[接入指导手册](INTEGRATION.md)** —— 那里有每一步的判据和真实踩过的坑。
