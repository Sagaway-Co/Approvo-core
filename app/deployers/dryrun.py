"""DryrunDeployer - 不连集群、不触发 GitHub,假装部署成功.

用途:跑通"申请→审批→结果卡片"整条链路,或联调用.
"""
from app import deployer as _legacy
from app.deployers import Deployer


class DryrunDeployer(Deployer):

    def deploy(self, spec: dict) -> tuple[bool, str]:
        return _legacy.deploy({**spec, "method": "dryrun"})
