# Architecture

approvo 的核心承诺是 **"审批通过之前不允许部署,审批通过之后一定部署且只部署一次"**.本文说明这个承诺是怎么落地的.

## 组件图

```
                    ┌────────────────────────────────────────┐
                    │              approvo (单副本)              │
                    │                                            │
    业务仓 CI ──POST /release──►  ┌─────────────┐                 │
                    │             │  server.py  │                 │
                    │             │  FastAPI    │                 │
                    │             └──────┬──────┘                 │
                    │                    │                        │
                    │                    ▼                        │
                    │            ┌───────────────┐                │
                    │            │   store.py    │  SQLite (PVC)  │
                    │            │  CAS + 状态机  │                │
                    │            └───────┬───────┘                │
                    │                    │                        │
    飞书长连接 ──────►  ┌──────────┐      │      ┌──────────────┐  │
                    │ │events.py │─────►│     │  cards.py    │  │
                    │ │ handler  │      │     │  render      │  │
                    │ └────┬─────┘      │     └──────┬───────┘  │
                    │      │            │            │          │
                    │      └────────────┼────────────┘          │
                    │                   │                       │
                    │  ┌────────────────┼─────────────────────┐ │
                    │  │       providers/feishu.py             │ │
                    │  │   (审批 API + 消息 API + 群成员 API)     │ │
                    │  └────────────────┬─────────────────────┘ │
                    │                   │                       │
                    │  ┌────────────────┼─────────────────────┐ │
                    │  │        deployers/{github,kubectl,     │ │
                    │  │          helm,dryrun}.py              │ │
                    │  └────────────────┼─────────────────────┘ │
                    │                   │                       │
                    │  ┌────────────────┴─────────────────────┐ │
                    │  │    sources/github.py                  │ │
                    │  │    (compare API + PR 归属)             │ │
                    │  └───────────────────────────────────────┘ │
                    │                                            │
                    │  启动时对账一次 pending 实例 (兜底)            │
                    └────────────────────────────────────────────┘
                                        │
                                        ▼
              method=github:  workflow_dispatch → 受限发版仓 deploy.yml
              method=kubectl: kubectl set image + rollout
              method=helm:    helm upgrade --reuse-values --set image.tag=...
              method=dryrun:  不连集群,直接返回 ok
```

## 端到端时序

```
CI                approvo               飞书                 群成员              deploy
│  POST /release   │                    │                    │                   │
├─────────────────►│                    │                    │                   │
│                  │ list_chat_members  │                    │                   │
│                  ├───────────────────►│                    │                   │
│                  │                    │                    │                   │
│                  │ create_instance     │                    │                   │
│                  │ (approvers=群成员)   │                    │                   │
│                  ├───────────────────►│                    │                   │
│                  │  instance_code     │                    │                   │
│                  │◄───────────────────┤                    │                   │
│                  │  send submit_card  │                    │                   │
│                  ├───────────────────►│──── 审批卡片 ──────►│                   │
│  200 OK          │  (SQLite: pending) │                    │                   │
│◄─────────────────┤                    │                    │                   │
│                  │                    │                    │  点通过           │
│                  │                    │◄───────────────────┤                   │
│                  │  approval_instance │                    │                   │
│                  │◄───────────────────┤ (长连接事件)         │                   │
│                  │                    │                    │                   │
│                  │  try_claim_for_    │                    │                   │
│                  │  deploy (CAS)      │                    │                   │
│                  │  pending→deploying │                    │                   │
│                  │                    │                    │                   │
│                  │  send deploying_   │                    │                   │
│                  │  card              │                    │                   │
│                  ├───────────────────►│                    │                   │
│                  │                    │                    │                   │
│                  │  deploy(spec)                                                │
│                  ├────────────────────────────────────────────────────────────►│
│                  │                                                              │
│                  │  部署完成                                                     │
│                  │◄────────────────────────────────────────────────────────────┤
│                  │  SQLite: success                                             │
│                  │                                                              │
│                  │  send result_card  │                                        │
│                  ├───────────────────►│                                        │
```

## 关键组件

### server.py — HTTP 入口

- `POST /release` — CI 调用,建审批实例 + 落库
- `POST /release` **限流**:每分钟 60 次全局(单副本,内存 deque)
- `POST /release` **鉴权**:X-Release-Token header 与环境变量 `RELEASE_TOKEN` `secrets.compare_digest` 对比
- `POST /release` **防重与幂等**:
  - 同 tag 有 `pending`/`deploying` → 409 拒绝
  - 同 tag 最近状态是 `success` → 200 `{skipped: "already deployed"}`
  - 其它 (rejected / failed / canceled) → 允许新申请,uuid 用历史次数区分避免飞书 60012 冲突
- `/admin` — Basic Auth 管理页,维护 GitHub 用户名 → 飞书 user_id 映射;`ADMIN_PASSWORD` 未设即禁用

### store.py — SQLite 状态机

两张表:
- **releases**:`instance_code (pk), repo, tag, image, cluster, namespace, spec_json, status, created_at, updated_at`
- **user_map**:`github_login (pk), user_id, name, updated_at`

状态流转:`pending → deploying → success | failed`(或 `rejected | canceled | deleted`).

**`try_claim_for_deploy(instance_code)`** — 原子 CAS:
```sql
UPDATE releases SET status='deploying', updated_at=? 
  WHERE instance_code=? AND status='pending'
```
返回 `rowcount == 1` 才算抢到.这样即使长连接事件重投或轮询同时命中,也只有一个线程会走到 `deploy(spec)`.

### events.py — 事件处理 + 启动对账

- **长连接 handler** (`build_handler`):`approval_instance` 事件 → `process_instance(instance_code)`
- **reconcile_pending_once**:**进程启动时**扫一遍 `store.list_pending()`,对每个 pending 实例调 `feishu.get_instance` 拉真实状态,补处理离线期间漏掉的事件

**为什么不再每 60s 轮询?** 轮询是按 pending 条数 × 分钟数计费的:审批"等人点通过"往往要几小时,
一条审批过一夜≈近千次 API 调用,而这些调用**什么也没发现**——实时性本来就由长连接推送保证。
实测这一项几乎吃光了 IM 租户的基础 API 月额度,于是改为**只在启动时对账一次**:
成本 = 启动瞬间的 pending 条数(通常为 0),稳态调用量接近 0.

⚠️ **残留风险(已知,写在代码注释里)**:若在**运行中**长连接恰好抖动的那几秒里审批被决策,
该事件可能丢,这条 release 会一直 pending 且不部署 —— 当前靠"下次重启时的对账"补上。
要彻底覆盖应在长连接**重连**回调里再对账一次,而不是退回按分钟轮询。

### providers/feishu.py + app/feishu.py — 飞书 API 封装

- `subscribe(approval_code)` — 启动时订阅,幂等(重复订阅 code 1390007 也接受)
- `create_instance(...)` — 建审批实例;`node_approver_user_id_list` 传当前群成员做 Free 节点动态审批人
- `get_instance(instance_code)` — 拉状态 + timeline
- `send_card(chat_id, card)` — 发交互卡片
- `get_chat_members(chat_id)` — 分页拉群成员 (机器人无 user_id 会被过滤)
- `user_id_by_email(email)` — 邮箱反查

**token 缓存**:`tenant_access_token` 2h 有效期,过期前 60s 自动续.

### deployers/*.py — 4 种部署方式

见 [DEPLOYERS.md](DEPLOYERS.md).

### sources/github.py — 变更清单

- `release_changes(source_repo, base, head)` — GitHub compare API 拉 commits
- PR 归属两层提取:先 commit message 正则 (`(#123)` / `Merge pull request #123`) 匹配;未命中的调 `/commits/{sha}/pulls`(rebase merge 兜底)
- 仍未归属的列为"未关联 PR 的直接提交"
- 用**只读 App token**(缩到目标源码仓 + `contents:read + pull_requests:read`),按仓库缓存 50min

## 凭证隔离设计

approvo 的部署路径设计成 **method=github**:approvo 不持 kubeconfig / cloud AK,只持 GitHub App private key + `actions:write` for 受限发版仓.
真实部署凭证 (kubeconfig / AWS AK / helm cluster context) 只以 GitHub Secrets 形式存在于受限发版仓中,只有少数运维/负责人可写.

即使业务仓开发拿到了业务仓的写权限,也:
- 无法从业务仓偷出部署凭证 (业务仓根本没这些)
- 无法通过修改业务仓 CI 绕过审批 (approvo 是唯一部署入口)

## 单副本决策

SQLite + 长连接决定单副本:
- SQLite 不支持多副本并发写
- 长连接事件如果被多副本接收会重复处理
- CAS 只在单进程内原子

Trade-off:牺牲横向 HA,换来极简运维.**回避方式**:未来加 Postgres store backend + Redis pub/sub 事件分发,可支持多副本.

## 数据流小结

1. **配置**只加载一次 (`settings.py`);热改配置需重启
2. **user_map** 首次启动灌初始值,之后以 SQLite 为准(可 /admin 热改)
3. **spec_json** 完整存部署规格,部署时回放所有参数,无需回查 config
4. **变更清单** 基准是"上次成功部署的 commit" (store.last_success_commit),只有 approvo 知道线上真正部署到哪个版本
