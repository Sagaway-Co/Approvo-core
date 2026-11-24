"""回滚请求：把指定服务切回历史版本的镜像，走与发版相同的审批，审批通过后派发给部署流水线。

请求体 `rollback` 字段（与 tag 必须相同的 id）：
  id:                  "RB-xxx"，回滚单号，必须等于请求的 tag
  services:            {服务名: 目标版本 tag}，至少一项
  configmaps:          可选，[{path, from}]：把 path 恢复为 from 版本的内容
  allow_schema_behind: 目标版本之后含迁移时必须显式为 true（默认拒绝）
  notes:               可选，最长 300 字

这里只做结构校验与展示/派发文本。版本是否存在、是否早于最新版、迁移检查都由部署流水线做（fail-close）。
"""
import json
import re

ID_RE = re.compile(r"^RB-[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
PATH_MAX = 256
MAX_ITEMS = 20
NOTES_MAX = 300
_ALLOWED = {"id", "services", "configmaps", "allow_schema_behind", "notes"}


class RollbackError(ValueError):
    pass


def validate(raw, tag: str) -> dict:
    """→ 规范化后的回滚请求；不合法抛 RollbackError。"""
    if not isinstance(raw, dict):
        raise RollbackError("rollback 必须是对象")
    unknown = set(raw) - _ALLOWED
    if unknown:
        raise RollbackError(f"不认识的字段：{', '.join(sorted(map(str, unknown)))}")
    rid = raw.get("id")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        raise RollbackError(f"id 必须形如 RB-xxx：{rid!r}")
    if tag != rid:
        raise RollbackError("请求的 tag 必须与 rollback.id 相同")

    services = raw.get("services")
    if not isinstance(services, dict) or not services or len(services) > MAX_ITEMS:
        raise RollbackError("services 必须是 1~20 项的映射：服务名 → 目标 tag")
    for name, t in services.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise RollbackError(f"服务名不合法：{name!r}")
        if not isinstance(t, str) or not TAG_RE.match(t):
            raise RollbackError(f"服务 {name} 的目标 tag 不合法：{t!r}")

    cms_raw = raw.get("configmaps") or []
    if not isinstance(cms_raw, list) or len(cms_raw) > MAX_ITEMS:
        raise RollbackError("configmaps 必须是列表（最多 20 项）")
    cms = []
    for item in cms_raw:
        if not isinstance(item, dict) or set(item) != {"path", "from"}:
            raise RollbackError("configmaps 每项只能是 {path, from}")
        p, f = item["path"], item["from"]
        if not isinstance(p, str) or not p or len(p) > PATH_MAX or p.startswith("/") or ".." in p.split("/"):
            raise RollbackError(f"configmap 路径不合法：{p!r}")
        if not isinstance(f, str) or not TAG_RE.match(f):
            raise RollbackError(f"configmap {p} 的来源 tag 不合法：{f!r}")
        cms.append({"path": p, "from": f})

    allow = raw.get("allow_schema_behind", False)
    if not isinstance(allow, bool):
        raise RollbackError("allow_schema_behind 只能是 true / false")
    notes = raw.get("notes") or ""
    if not isinstance(notes, str) or len(notes) > NOTES_MAX:
        raise RollbackError(f"notes 必须是不超过 {NOTES_MAX} 字的字符串")
    return {"id": rid, "services": dict(sorted(services.items())), "configmaps": cms,
            "allow_schema_behind": allow, "notes": notes}


def summary_lines(rb: dict) -> list:
    """审批卡上显示的几行文字。"""
    svc = "，".join(f"{n} → {t}" for n, t in rb["services"].items())
    cms = "，".join(f"{c['path']}（取自 {c['from']}）" for c in rb["configmaps"]) or "不改"
    behind = "已允许（审批人在请求中显式开启）" if rb["allow_schema_behind"] else "默认拒绝"
    lines = [f"回滚单号 {rb['id']}", f"服务：{svc}", f"ConfigMap：{cms}",
             "迁移：不执行（回滚只切镜像）", f"目标之后的版本含迁移：{behind}"]
    if rb.get("notes"):
        lines.append(f"说明：{rb['notes']}")
    return lines


def dispatch_input(rb: dict) -> str:
    """派发给部署 workflow 的 rollback_spec 输入（JSON 文本）。"""
    return json.dumps(rb, ensure_ascii=False, sort_keys=True)
