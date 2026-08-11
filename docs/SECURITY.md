# Security Notes

**不是 vulnerability disclosure policy**(那在项目根的 [`SECURITY.md`](../SECURITY.md)),本文说明 approvo 的**架构级安全边界**——你在设计部署形态时需要知道的信息.

## 威胁模型

| 攻击面 | 谁能碰 | 缓解 |
| --- | --- | --- |
| `/release` HTTP 接口 | 拿到 `RELEASE_TOKEN` 的攻击者 | `secrets.compare_digest` 常时对比;限流 60/min |
| `/admin` 页 | 拿到 `ADMIN_PASSWORD` 的攻击者 | Basic Auth;`ADMIN_PASSWORD` 未设即禁用整个 admin |
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

- **公网 Ingress** — 只暴露 `/release` + `/healthz`.**`/admin`、`/api/*` 在这条 Ingress 上没有路由**,公网够不到 (结构性隔离,不依赖认证)
- **内网 Ingress** — 全部路径 (含 `/admin`),仅内网 DNS 解析

即使 `RELEASE_TOKEN` 泄漏,攻击者也无法调 `/admin` 修改 user_map.

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

60s poller 也调 `process_instance`,同样受 CAS 保护.

## 已知安全边界

### SQLite 单副本

- 单节点故障 = 断服.**没有 HA**.
- PVC 建议定期快照 (velero / cluster snapshot)
- 未来加 Postgres backend 支持多副本 HA (在 [CHANGELOG.md](../CHANGELOG.md) unreleased 节)

### 长连接故障恢复

- 长连接断开时,飞书**不会重投**期间发生的事件
- approvo poller 每 60s 对账 pending 实例,能补大部分漏事件
- 极端场景:长连接持续断 60s 以上 → 事件确实丢 → poller 兜底恢复

### 秘钥轮换

- 飞书 App Secret / GitHub App private key / `RELEASE_TOKEN` / `ADMIN_PASSWORD` **必须定期轮换**(建议每 90 天)
- 轮换步骤:改 secret → `kubectl rollout restart deploy/approvo` → 验证 `/healthz`
- 长连接会自动重连 (新 secret 生效)

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
