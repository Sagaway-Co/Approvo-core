# Deployers

approvo 通过 `Deployer` ABC 支持多种部署方式.

## 内置 Deployer 对比

| method | 凭证位置 | 集群交互 | 适合 |
| --- | --- | --- | --- |
| **github** | 受限发版仓 GitHub Secrets | 无 (走 GitHub Actions) | 生产 (**推荐**):凭证隔离最彻底 |
| **kubectl** | approvo 挂载的 kubeconfig | 直连 API server | 内网集群 + 单一环境;approvo 只对目标 ns 有 rollout 权限 |
| **helm** | approvo 挂载的 kubeconfig | 直连 API server | 你已经用 helm 部署;`helm upgrade --reuse-values` 保留其他配置 |
| **dryrun** | 无 | 无 | 联调链路:跑通"申请→审批→部署→结果卡" |

## Deployer 契约

定义在 [`app/deployers/__init__.py`](../app/deployers/__init__.py):

```python
class Deployer(ABC):
    @abstractmethod
    def deploy(self, spec: dict) -> tuple[bool, str]:
        """执行部署,返回 (ok, log).spec 结构见 CONFIGURATION.md"""

def get_deployer(method: str) -> Deployer:
    ...  # 按 method 分派
```

`spec` 由 `app/server.py` 组装,包含 `repo` / `tag` / `image` / `cluster` / `namespace` / `deployment` / `container` / `helm_release` / `chart` / `github` (dict) / `commit` / ... 全部字段.

## github deployer 详解

流程:
1. 构造 workflow_dispatch inputs:`{app: <repo>, tag: <tag>, env: <env>, ...spec.github.inputs}`
2. POST `/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches`,ref = `spec.github.ref`
3. 轮询 20 次 (每次 3s) 查 `workflow_runs?event=workflow_dispatch`,按 `display_title` 定位刚触发的 run
4. 轮询等 run `status=completed` (最长 900s)
5. 读 `conclusion` (success / failure / cancelled)
6. 失败时抓 jobs/steps,列出失败步骤

**受限发版仓的 deploy.yml 必须**:
- `on: workflow_dispatch` + inputs `app` / `tag` / `env`
- **`run-name` 格式**:`deploy {app} {tag} ({env})` (approvo 按这个定位)
- 用 GitHub Secrets 里的凭证 (kubeconfig / cloud AK) 执行真实部署

模板:[examples/restricted-deploy-repo/deploy.yml.template](../examples/restricted-deploy-repo/deploy.yml.template)

## kubectl deployer 详解

流程:
1. 从 `spec.cluster` 找到 kubeconfig 挂载路径 + context (`settings.CLUSTERS`)
2. `kubectl set image deployment/<deploy> <container>=<image>` (超时 120s)
3. `kubectl rollout status deployment/<deploy> --timeout=300s`
4. 失败时补抓:pod 状态 (`get pods` 前缀过滤) + deployment describe 末 15 行

**权限最小化**:kubeconfig 里的 SA 只对目标 ns 的 deployment 有 `patch` + `rollout` 权限,不要给 admin.

**注意**:容器名可能 != deployment 名 (helm chart 生成的容器名带前缀).`container` 字段单独配.

## helm deployer 详解

命令:
```
helm --kube-context <ctx> upgrade <release> <chart> \
  -n <ns> --reuse-values \
  --set <image_key>=<tag> \
  --wait --timeout 5m
```

**`--reuse-values`** 保留其他 values (只覆盖 image.tag).**chart** 必须是容器内可访问的路径或 helm repo ref.

## dryrun deployer 详解

不连集群,返回固定 log:

```
[dryrun] 不会真部署。
目标: <cluster>/<ns> deploy=<deployment or helm_release>
镜像: <image>
$ (kubectl set image / helm upgrade 在这里被跳过)
deployment successfully rolled out  # 假的
```

用于:
- 联调飞书审批链路 (不需要真集群)
- 教程 / 演示

## 新增 Deployer 步骤

### 1. 建文件

```
app/deployers/argocd.py
```

```python
from app.deployers import Deployer

class ArgocdDeployer(Deployer):
    def deploy(self, spec: dict) -> tuple[bool, str]:
        # argocd app sync <app> --revision <tag> --wait
        ...
        return ok, log
```

### 2. 注册到 factory

修改 [`app/deployers/__init__.py`](../app/deployers/__init__.py):

```python
def get_deployer(method: str) -> Deployer:
    ...
    if method == "argocd":
        from app.deployers.argocd import ArgocdDeployer
        return ArgocdDeployer()
    ...
```

### 3. 文档

- 在本文件加入"argocd deployer 详解"节
- 在 [CONFIGURATION.md](CONFIGURATION.md) 加入 argocd 特定的 spec 字段
- 在 [config.example.yaml](../config.example.yaml) 加个示例 release

### 4. 测试

- dryrun 走通链路
- 真实场景至少手动跑 3 次:成功 / 失败 (集群不可达) / 超时

## 未来 Deployer 想法

- **argocd**:`argocd app sync <name> --revision <tag>`,ApplicationSet 场景
- **ssh**:`ssh <host> "docker pull && docker restart"`,老式服务器场景
- **flux**:`flux reconcile helmrelease <name>`
- **cloudrun / lambda**:通过对应 SDK 更新 revision
- **blue-green**:接 Argo Rollouts,`kubectl argo rollouts promote`
