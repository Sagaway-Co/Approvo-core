"""部署执行器抽象层.

现有内置 4 种 deployer:
- github  - workflow_dispatch 触发受限发版仓 (推荐:凭证隔离)
- kubectl - kubectl set image + rollout status
- helm    - helm upgrade --reuse-values --set image.tag=<tag>
- dryrun  - 不连集群,仅走通链路;联调用
- grant   - 授权类申请(如两步式 Secret 变更):审批通过时不做任何集群操作

新增 deployer 请见 docs/DEPLOYERS.md.
"""
from abc import ABC, abstractmethod


class Deployer(ABC):
    """部署契约.实现类 deploy(spec) 返回 (ok: bool, log: str)."""

    @abstractmethod
    def deploy(self, spec: dict) -> tuple[bool, str]:
        """执行部署,返回 (ok, log).spec 结构见 docs/CONFIGURATION.md."""


def get_deployer(method: str) -> Deployer:
    """按 spec.method 选 deployer."""
    if method == "github":
        from app.deployers.github import GithubDeployer
        return GithubDeployer()
    if method == "kubectl":
        from app.deployers.kubectl import KubectlDeployer
        return KubectlDeployer()
    if method == "helm":
        from app.deployers.helm import HelmDeployer
        return HelmDeployer()
    if method == "grant":
        from app.deployers.grant import GrantDeployer
        return GrantDeployer()
    if method == "dryrun":
        from app.deployers.dryrun import DryrunDeployer
        return DryrunDeployer()
    raise ValueError(f"unknown deploy method: {method}")
