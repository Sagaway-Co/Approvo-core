"""环境变量 + config.yaml 加载。

密钥走环境变量(别进 git),业务映射走 config.yaml。
"""
import os
from zoneinfo import ZoneInfo

import yaml

CN_TZ = ZoneInfo("Asia/Shanghai")   # 所有展示时间用上海时区(容器默认 UTC)

APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]

# 飞书(国内) vs Lark(海外) endpoint. 取值:
#   "feishu" (默认)  -> https://open.feishu.cn
#   "lark"          -> https://open.larksuite.com
#   或直接完整 URL(自建/私有部署)
_endpoint = os.environ.get("LARK_ENDPOINT", "feishu").strip().lower()
if _endpoint in ("", "feishu"):
    LARK_DOMAIN = "https://open.feishu.cn"
elif _endpoint == "lark":
    LARK_DOMAIN = "https://open.larksuite.com"
elif _endpoint.startswith(("http://", "https://")):
    LARK_DOMAIN = _endpoint.rstrip("/")
else:
    raise ValueError(f"LARK_ENDPOINT 无效: '{_endpoint}';支持 feishu/lark 或完整 URL")

# /release 接口的共享密钥,CI 调用时带在 X-Release-Token 头里。留空=不校验(仅内网时可接受)
RELEASE_TOKEN = os.environ.get("RELEASE_TOKEN", "")

# 对外可访问的 approvo 入口。【仅】用于在提示信息里拼出可复制的 curl 命令,不参与任何鉴权。
# 刻意不给出真实默认值:把部署地址写死进代码,既是信息泄露也让别人没法直接用。
PUBLIC_GATE_URL = os.environ.get("PUBLIC_GATE_URL", "https://<your-approvo-host>").rstrip("/")

# /admin 管理页的 Basic Auth。不设 ADMIN_PASSWORD 则禁用管理页(避免裸奔的身份编辑器)
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")
DB_PATH = os.environ.get("DB_PATH", "release.db")   # 仅迁移脚本用(读旧 SQLite)
# 状态库(PostgreSQL)。2026-08-10 起 approvo 的状态存 RDS,容器本身无状态。
# 🔴 没有它 approvo 无法工作(状态丢=批了不部署),所以【不提供默认值】:
#    缺配置就该在启动时清楚地失败,而不是静默退回某个本地文件继续跑。
# 形如 postgresql://user:pass@host:5432/approvo   —— 值走环境变量,不进 git。
DB_DSN = os.environ.get("DB_DSN", "")
PORT = int(os.environ.get("PORT", "8700"))

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

DEFAULT_PROJECT = CFG.get("default_project", "")   # 卡片标题前缀; release/req 可覆盖

_f = CFG["feishu"]
# 两种方式配审批定义(至少一个):
#   1) approval_code: <str>          - 单定义,所有 stage 共用(小团队/简单场景)
#   2) approval_codes: {stage: code} - 按 stage 分派 (pre/release/hotfix 用不同审批人)
#      未匹配的 stage 会 fallback 到 approval_codes.default 或 approval_code
APPROVAL_CODE = _f.get("approval_code")
APPROVAL_CODES = _f.get("approval_codes") or {}
if not (APPROVAL_CODE or APPROVAL_CODES):
    raise RuntimeError("feishu.approval_code 或 feishu.approval_codes 至少配一个")


def approval_code_for(stage: str | None) -> str:
    """按 stage 选审批定义 code. stage 未传或未匹配时回落到默认."""
    if stage and APPROVAL_CODES.get(stage):
        return APPROVAL_CODES[stage]
    if APPROVAL_CODES.get("default"):
        return APPROVAL_CODES["default"]
    if APPROVAL_CODE:
        return APPROVAL_CODE
    raise RuntimeError(f"没有匹配 stage='{stage}' 的 approval_code,且无默认值")

NOTIFY_CHAT_ID = _f.get("notify_chat_id")          # 旧字段,兜底
DETAIL_CHAT_ID = _f.get("detail_chat_id") or NOTIFY_CHAT_ID   # 审批群:详细申请/待审批卡
RESULT_CHAT_ID = _f.get("result_chat_id") or NOTIFY_CHAT_ID   # 结果群:只发版本已部署/拒绝卡
DEFAULT_INITIATOR = _f.get("default_initiator_user_id")  # 解析不出发起人时兜底
# 逻辑字段名 -> 审批定义里的真实控件 id。UI 手搓的定义在这里填 GUID;
# 用 create_approval.py 建的可不填(id 就是逻辑名)。
FORM_FIELD_IDS = _f.get("form_field_ids", {})
# github 用户名 -> 飞书 user_id。让审批的「申请人」是真正推 tag 的那个人。
USER_MAP = _f.get("user_map", {})

CLUSTERS = CFG.get("clusters", {})   # name -> {kubeconfig, context};github 方式可不配
# 只读凭证自助面板上的按钮。每项:{label, cluster, namespace, minutes, prod}
# 不配则 /viewer-panel 拒绝发卡(而不是发一张没有按钮的空卡)。
VIEWER_TARGETS = CFG.get("viewer_targets", [])
RELEASES = CFG["releases"]           # repo -> 部署目标
