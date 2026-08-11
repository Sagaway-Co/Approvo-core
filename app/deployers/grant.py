"""GrantDeployer —— 「授权类」申请的执行器：审批通过时【什么都不做】。

用于两步式 Secret 变更：审批批的是「允许改 Secret X 的 key Y」这件事本身，
而不是某个具体的值。值在审批通过【之后】由申请方单独提交（POST /secret/commit），
故此处只把状态推进到 success，表示"授权已生效，等待提交值"。

为什么值不随审批走：审批是异步的，approvo 必须把值存住等人来批，而 spec 会序列化
进 release.db（PVC 上，任何能 exec 进 approvo 的人都读得到）。两步式让值
【全程不落库】。反正审批人本来也看不到值，批的实质就是"允许改这个 key"。
"""
from app.deployers import Deployer


class GrantDeployer(Deployer):

    def deploy(self, spec: dict) -> tuple[bool, str]:
        return True, (
            "已授权：允许修改 "
            f"{spec.get('namespace')}/{spec.get('deployment')} 的 key "
            f"`{spec.get('image_key')}`。\n"
            "⏳ 请在授权有效期内提交值（POST /secret/commit），"
            "逾期或用过一次即失效。值不会经过审批流、不落库。"
        )
