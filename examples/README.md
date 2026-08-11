# approvo 接入示例

本目录提供两个可复制的接入模板,展示 approvo 与业务仓的边界:

```
业务仓 (开发可写)                 受限发版仓 (仅少数人可写)
├── .github/workflows/            ├── .github/workflows/
│   └── release.yml               │   └── deploy.yml
│      ↓ POST /release            │      ↑ workflow_dispatch
└──── 建议:构建镜像+curl approvo   └── 建议:kubectl set image + rollout
                                   持有真实 kubeconfig / cloud creds

                approvo (中间路由)
                ├── 收 POST /release
                ├── 落库 + 建审批
                ├── 长连接收审批结果
                └── 通过后 workflow_dispatch → 受限仓
```

## caller-workflows/

放到**业务仓**的 `.github/workflows/` 下.业务仓无需持有部署凭证.

- [`build-and-request-approval.yml`](caller-workflows/build-and-request-approval.yml) — 完整流程模板:tag 触发 → 构建镜像 → curl approvo `/release`.**fail-close**:如果 approvo 不可达,workflow 失败,阻止无审批发版.

## restricted-deploy-repo/

放到**受限发版仓**的 `.github/workflows/` 下.所有部署凭证(kubeconfig / cloud AK/SK)以 GitHub Secrets 形式**只**存在该仓库,开发无写权限.

- [`deploy.yml.template`](restricted-deploy-repo/deploy.yml.template) — 被 approvo `workflow_dispatch` 触发的部署 workflow.按 `app` 参数 case 分支解析部署目标.

## 配套 approvo config

见项目根目录 [`config.example.yaml`](../config.example.yaml).关键点:
- 每个 release 的 `method: github` + `github.repo` 指向受限发版仓
- `image_repo` 只用于展示(不影响部署)
