"""环境变量 + config.yaml 加载。

密钥走环境变量(别进 git),业务映射走 config.yaml。
"""
import os
import re
from datetime import datetime
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


# ── 审批直通开关(应急旁路)────────────────────────────────────────────
# 场景:审批平台(飞书/Lark)不可用时 —— 典型是 API 调用额度耗尽 ——
#       建审批实例 / 拉群成员 / 发卡这三处调用全部失败 → 审批根本发不出去 →
#       QA 和生产都卡在"提审批"这一步,发不了版。
#       这是一个【应急总开关】,平时必须为 off。
#
# 打开后按环境【非对称】分流(qa 放行、生产收紧),见 server._bypass_release:
#   env=qa   → 跳过审批,直接跑原有部署流程(秒通过);少的只是"人点通过"这一下
#   env≠qa   → 只落库留痕 + 状态直接置 failed + 【不部署】,由人工线下执行
#              (生产绝不静默上一个没人批过的版本 —— fail-close)
#
# 值语义(env APPROVAL_BYPASS):
#   · 空 / 缺省 / 格式非法          → 关(默认安全,fail-close)
#   · "YYYY-MM"(如 2026-08)        → 仅在该【自然月】生效,跨月即自动失效
#
# 🔴 为什么用带年月的字符串、而不是简单的 1/true:
# 应急旁路本就只该活到当月月底(IM 平台的调用额度通常按自然月重置)。把"授权月"
# 写进值里,开关就【自带过期】——到下月 1 号 0 点,当前月 != 授权月,自动回到
# "需审批"(生产也一并恢复),不依赖任何人记得去删变量。判月用 CN_TZ
# (与额度重置口径一致的时区)。要下个月继续用,必须显式改成新月份 →
# 每月一次的"重新确认",与应急语义相符。
#
# 为什么用【环境变量】而不是 config.yaml:这是 kill-switch,要能脱离业务配置快速
# 开合;且刻意【不】常驻进 git 里的环境 values 文件(那样容易忘记关)。
# 放环境变量则"改 deployment env → pod 重启即生效",天然不落 git;跨月又会自动失效,
# 双保险。每次调用都【实时读 env + 实时取当前月】(不在 import 时定值),否则长驻进程
# 跨月后仍会用旧月份,自动失效就成了空话。
def approval_bypass_on() -> bool:
    raw = os.environ.get("APPROVAL_BYPASS", "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", raw):   # 只认 YYYY-MM;含 1/true/空/格式错一律关
        return False
    now = datetime.now(CN_TZ)
    return raw == f"{now.year:04d}-{now.month:02d}"


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
# 只读凭证自助面板上的按钮。每项:{label, cluster, namespace, minutes, prod, sa}
# 不配则 /viewer-panel 拒绝发卡(而不是发一张没有按钮的空卡)。
VIEWER_TARGETS = CFG.get("viewer_targets", [])
# 只读 SA 名的【全局兜底】。刻意没有内置默认值:一个 namespace 里该用哪个只读账号,
# 平台猜不出来 —— 猜错的后果是"拿着另一个环境的账号名去签凭证",而万一同名 SA
# 恰好存在,权限就不是你以为的那个。优先级:target.sa > viewer_sa_default > 拒绝签发。
VIEWER_SA_DEFAULT = CFG.get("viewer_sa_default") or ""
# 机器人菜单目标(event_key -> 动作)。配置里给了就用配置,否则回落到 events.py 的
# 内置示例表。做成可配是为了让"开通一个新环境"不必改代码。
MENU_TARGETS = CFG.get("menu_targets") or {}
# Secret 变更通道的目标白名单。与 viewer_targets 同构,额外支持 release_key
# (让不同项目的 Secret 变更用各自的 release 条目 → 卡片能显示是谁的变更)。
# 🔴 刻意【不给默认值】:空 = 通道整体关闭。原实现根本没有白名单,
#    cluster/namespace 是请求体里任意可填的 —— 而 /viewer-token 是有白名单的,
#    同一类"改集群"的能力判据不对称,违反"同一能力的多个入口必须共用同一份判据"。
SECRET_TARGETS = CFG.get("secret_targets") or []
# 部署凭据的目标白名单。fail-close:不配即整条通道关闭。
# 每项 {cluster, namespace, sa};sa 必须显式写 —— 不猜(见 viewer_sa_for 的同类教训)。
DEPLOY_TARGETS: list = CFG.get("deploy_targets") or []
# 未在某个 secret target 上指定 release_key 时用它(要在 releases 里有同名条目)。
SECRET_RELEASE_KEY = CFG.get("secret_release_key", "my-secret")
RELEASES = CFG["releases"]           # repo -> 部署目标
