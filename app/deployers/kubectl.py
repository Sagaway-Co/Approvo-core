"""KubectlDeployer - kubectl set image + rollout status,并在失败时补抓 pod 状态/事件.

薄 wrapper 调用 app.deployer.deploy(),A3 阶段零逻辑改动.
"""
from app import deployer as _legacy
from app.deployers import Deployer


class KubectlDeployer(Deployer):

    def deploy(self, spec: dict) -> tuple[bool, str]:
        return _legacy.deploy({**spec, "method": "kubectl"})
