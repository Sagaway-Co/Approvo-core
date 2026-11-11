"""发布清单(release manifest):让审批卡【精确到服务】地说明这次发什么。

🔴 为什么需要它:一个仓库里放着多个服务(monorepo)时,卡片上的「应用」只是仓库名、
   「镜像」只是 `image_repo:tag` —— 审批人看不出这次到底换哪几个服务的镜像、
   跑不跑迁移、改不改配置。而这些信息就写在源码仓里、跟着 tag 一起不可变:
   发布流水线按它部署,审批卡就应该按【同一份文件、同一个 commit】展示。

配置(可选,没配就整块不渲染,行为与从前一致):

    releases:
      my-monorepo:
        release_manifest:
          repo: my-monorepo                    # 省略则用 source_repo
          path: "releases/{tag}/release.yaml"  # 必须含 {tag}
          registry: "deploy/services.yaml"     # 可选:服务登记表,补出镜像名与工作负载名

清单文件(YAML):

    services: [svc-a, svc-b]          # 本次发布的服务,按发布顺序;不发服务写 []
    migrations:                        # 可选;file 相对清单所在目录
      - {database: main_db, file: migrations/001_add_x.sql}
    configmaps: [deploy/cm/svc-a.yaml] # 可选;仓库内路径
    deployments: [deploy/app/svc-a.yaml]  # 可选;要同步环境变量的工作负载清单(只展示,执行在流水线)
    notes: 一句话说明                   # 可选

登记表(可选):`services.<name>.image` 与 `services.<name>.deploy[].deployment`。

⚖️ fail-close:配了清单却读不到 / 格式不对 / 引用的文件不存在 → 不发审批。
   审批人对着一张"说不清发什么"的卡点批准,比发不出审批更危险。

本模块只做纯解析与校验(不碰网络),拉文件在 github.release_manifest。
"""
from __future__ import annotations

import re

import yaml

MAX_BYTES = 64 * 1024            # 清单 / 单个迁移文件
REGISTRY_MAX_BYTES = 256 * 1024  # 服务登记表
MAX_SERVICES = 50
MAX_MIGRATIONS = 50
MAX_CONFIGMAPS = 50

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,199}")
_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PATH_SEG_RE = re.compile(r"[A-Za-z0-9._-]{1,200}")


class ManifestError(ValueError):
    """清单配置或内容不可用。server 映射成 409(人去改清单 / 配置),与 GitHub 不可达(502)区分。"""


def safe_rel_path(p: object, what: str) -> str:
    """仓库内相对路径:不许绝对路径、`..`、空段、奇怪字符 —— 它会被拼进 GitHub API 的 URL。"""
    if not isinstance(p, str) or not p or len(p) > 400:
        raise ManifestError(f"{what} 不是合法的仓库内路径:{p!r}")
    parts = p.split("/")
    if p.startswith("/") or any(x in ("", ".", "..") or not _PATH_SEG_RE.fullmatch(x) for x in parts):
        raise ManifestError(f"{what} 不是合法的仓库内路径:{p!r}")
    return p


def manifest_path(template: object, tag: str) -> str:
    if not isinstance(template, str) or "{tag}" not in template:
        raise ManifestError("release_manifest.path 必须是含 {tag} 的字符串")
    if not _TAG_RE.fullmatch(tag or ""):
        raise ManifestError(f"tag {tag!r} 含不允许的字符,拒绝拼进清单路径")
    return safe_rel_path(template.replace("{tag}", tag), "release_manifest.path")


def _load(text: str, what: str) -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ManifestError(f"{what} 不是合法的 YAML:{e}") from None
    if not isinstance(data, dict):
        raise ManifestError(f"{what} 顶层必须是映射")
    return data


def _list(data: dict, key: str, limit: int, required: bool = False) -> list:
    if key not in data:
        if required:
            raise ManifestError(f"清单缺少 {key}(不发服务也要显式写 {key}: [])")
        return []
    v = data[key]
    if v is None:
        return []
    if not isinstance(v, list):
        raise ManifestError(f"清单的 {key} 必须是列表")
    if len(v) > limit:
        raise ManifestError(f"清单的 {key} 超过 {limit} 项")
    return v


def parse_manifest(text: str) -> dict:
    """→ {"services": [name…], "migrations": [{database, file}…], "configmaps": [path…], "notes": str}"""
    data = _load(text, "发布清单")
    services = []
    for s in _list(data, "services", MAX_SERVICES, required=True):
        if not isinstance(s, str) or not _NAME_RE.fullmatch(s):
            raise ManifestError(f"清单 services 里有不合法的服务名:{s!r}")
        if s in services:
            raise ManifestError(f"清单 services 里 {s} 重复")
        services.append(s)
    migrations = []
    for m in _list(data, "migrations", MAX_MIGRATIONS):
        if not isinstance(m, dict) or set(m) - {"database", "file"}:
            raise ManifestError(f"清单 migrations 每项只能有 database / file:{m!r}")
        db = m.get("database")
        if not isinstance(db, str) or not _NAME_RE.fullmatch(db):
            raise ManifestError(f"清单 migrations 的 database 不合法:{db!r}")
        f = safe_rel_path(m.get("file"), "迁移文件")
        if not f.endswith(".sql"):
            raise ManifestError(f"迁移文件必须是 .sql:{f}")
        migrations.append({"database": db, "file": f})
    configmaps = [safe_rel_path(c, "ConfigMap 路径")
                  for c in _list(data, "configmaps", MAX_CONFIGMAPS)]
    # 可选:本次要同步环境变量(env / envFrom)的工作负载清单文件。只展示,执行与校验在部署流水线
    deployments = [safe_rel_path(d, "Deployment 清单路径")
                   for d in _list(data, "deployments", MAX_CONFIGMAPS)]
    notes = data.get("notes")
    if notes is not None and not isinstance(notes, (str, int, float)):
        raise ManifestError("清单的 notes 必须是一行文本")
    return {"services": services, "migrations": migrations, "configmaps": configmaps,
            "deployments": deployments, "notes": "" if notes is None else str(notes).strip()}


def parse_registry(text: str, services: list[str]) -> dict:
    """服务登记表 → {name: {"image": str|None, "deployments": [str…]}},只取本次清单里的服务。
    清单里的服务在登记表里找不到 → ManifestError:部署侧多半也会拒绝,审批卡不该先把它批出去。"""
    data = _load(text, "服务登记表")
    reg = data.get("services")
    if not isinstance(reg, dict):
        raise ManifestError("服务登记表缺少 services 映射")
    out = {}
    for name in services:
        ent = reg.get(name)
        if not isinstance(ent, dict):
            raise ManifestError(f"服务 {name} 没有在服务登记表里登记")
        image = ent.get("image")
        if image is not None and (not isinstance(image, str) or not _IMAGE_RE.fullmatch(image)):
            raise ManifestError(f"服务登记表里 {name} 的 image 不合法:{image!r}")
        deps = []
        for d in ent.get("deploy") or []:
            dep = d.get("deployment") if isinstance(d, dict) else None
            if isinstance(dep, str) and _NAME_RE.fullmatch(dep):
                deps.append(dep)
        out[name] = {"image": image, "deployments": deps}
    return out
