"""HelmDeployer - helm upgrade --reuse-values --set <image_key>=<tag>.

薄 wrapper 调用 app.deployer.deploy(),A3 阶段零逻辑改动.
"""
from app import deployer as _legacy
from app.deployers import Deployer


class HelmDeployer(Deployer):

    def deploy(self, spec: dict) -> tuple[bool, str]:
        return _legacy.deploy({**spec, "method": "helm"})
