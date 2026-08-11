"""GithubDeployer - 触发 workflow_dispatch 到受限发版仓,并等待 run 完成拿真实结果.

薄 wrapper 调用 app.github.dispatch_and_wait,A3 阶段零逻辑改动.
"""
from app import github
from app.deployers import Deployer


class GithubDeployer(Deployer):

    def deploy(self, spec: dict) -> tuple[bool, str]:
        return github.dispatch_and_wait(spec)
