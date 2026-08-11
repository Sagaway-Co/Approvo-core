"""HTTP 接口:CI 打 tag 后 POST /release,创建飞书审批实例并落库。

审批通过/拒绝由长连接事件驱动(见 events.py),不在这里处理。
"""
import hashlib
import json
import os
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from app import cards, feishu, forms, github, k8s, keygrant, settings, store


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """建表 + 首次灌初始映射。

    🔴 为什么从模块级挪到这里:原来 `store.init()` 写在模块顶层,于是【import 这个模块
    就会连数据库】。状态还在 SQLite(本地文件)时这几乎不会失败,所以一直没人发现;
    2026-08-10 迁到 PostgreSQL 后,同一行代码变成"没配 DSN 或库连不上就 import 失败",
    连累到任何只想引用一个函数的调用方(测试首先中招)。
    ⚖️ 判据:import 不该有副作用 —— 副作用要放在【应用启动】这个明确的时点。
    仍然是 fail-close:启动阶段连不上库就起不来,而不是带病运行。
    """
    store.init()
    store.usermap_seed(settings.USER_MAP)   # 首次启动从 config 灌初始映射,之后以 DB 为准
    yield


app = FastAPI(title="feishu-release-gate", lifespan=_lifespan)

_basic = HTTPBasic(auto_error=True)


def require_admin(cred: HTTPBasicCredentials = Depends(_basic)) -> str:
    """/admin 与映射 API 的 Basic Auth。未设 ADMIN_PASSWORD 则整体禁用。"""
    if not settings.ADMIN_PASSWORD:
        raise HTTPException(503, "管理页未启用:请设置环境变量 ADMIN_PASSWORD 后重启")
    ok = (secrets.compare_digest(cred.username, settings.ADMIN_USER)
          and secrets.compare_digest(cred.password, settings.ADMIN_PASSWORD))
    if not ok:
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Basic"})
    return cred.username


# /release 全局固定窗口限流(单副本,内存即可),防公网刷
_RL_MAX = int(os.environ.get("RELEASE_RATE_PER_MIN", "60"))
_rl_calls: deque = deque()


def _rate_ok() -> bool:
    now = time.time()
    while _rl_calls and now - _rl_calls[0] > 60:
        _rl_calls.popleft()
    if len(_rl_calls) >= _RL_MAX:
        return False
    _rl_calls.append(now)
    return True


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并 override 到 base:dict 递归合并,其他类型 override 胜。

    用于 release.stages 覆盖:让"一个应用一条 release",pre/release/hotfix
    通过 body.stage 分派 env / platform / github.env 等,而不需要拆多条。
    """
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class ReleaseReq(BaseModel):
    repo: str
    tag: str
    stage: str | None = None             # pre / release / hotfix, 决定审批人组合 + 卡片装饰
    env: str | None = None               # qa / prod, 覆盖 release 里的 env 展示
    project: str | None = None           # 项目名前缀,卡片标题显示 (覆盖 release + 全局默认)
    github_actor: str | None = None      # GitHub 用户名,经 user_map 解析为申请人
    operator_email: str | None = None
    operator_user_id: str | None = None
    commit: str | None = ""
    notes: str | None = ""


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def root():
    return RedirectResponse(url="/admin")


def _build_form(values: dict) -> str:
    """按 forms.FORM_FIELDS 生成创建实例用的 form。
    控件 id 用 config 里的映射(UI 手搓定义场景),没配就用逻辑名。"""
    out = []
    for key, _label, ctype in forms.FORM_FIELDS:
        out.append({
            "id": settings.FORM_FIELD_IDS.get(key, key),
            "type": ctype,
            "value": str(values.get(key, "")),
        })
    return json.dumps(out, ensure_ascii=False)


def _collect_changes(rel: dict, req: "ReleaseReq", head: str) -> dict | None:
    """变更清单:上次成功部署的 commit → 本次 commit 之间的 PR/直接提交。
    基准取 gate 自己的部署记录(CI 不知道也改不了线上版本),失败只降级提示,不阻塞审批。"""
    src = rel.get("source_repo")
    if not (src and head):
        return None   # 没配源码仓库 / 拿不到本次 commit:不渲染该区块
    base = store.last_success_commit(req.repo)
    if not base:
        return {"status": "first"}
    if base.startswith(head) or head.startswith(base):   # 长短 sha 混存,前缀即同版本
        return {"status": "nodiff", "prs": [], "direct_commits": []}
    try:
        ch = github.release_changes(src, base, head)
    except Exception as e:
        # 关键:把 exception 详情一并存进返回结构,写进 spec_json.changes 后
        # 事后可从 releases db 直接看根因(GitHub App 权限 / compare 404 / 网络),
        # 不再依赖 pod stdout log(pod 重启后就丢).
        err_msg = f"{type(e).__name__}: {e}"
        print(f"[changes] {req.repo}: compare {base}...{head} 失败: {err_msg}", flush=True)
        return {"status": "error", "error": err_msg, "base": base, "head": head, "source_repo": src}
    # PR/提交作者尽量映射成真名(user_map),审批人一眼看出「带没带别人的」
    for it in ch["prs"] + ch["direct_commits"]:
        row = store.usermap_get_row(it.get("author") or "")
        if row and row.get("name"):
            it["author"] = f'{row["name"]}({it["author"]})'
    return ch


@app.post("/release")
def release(req: ReleaseReq, x_release_token: str = Header(default="")):
    if not _rate_ok():
        raise HTTPException(429, "too many requests")
    if settings.RELEASE_TOKEN and not secrets.compare_digest(x_release_token, settings.RELEASE_TOKEN):
        raise HTTPException(401, "bad release token")

    rel = settings.RELEASES.get(req.repo)
    if not rel:
        raise HTTPException(404, f"repo '{req.repo}' 未在 config.releases 配置")

    # stages 覆盖: rel.stages.<req.stage> deep-merge 到顶层
    # 让"一条 release = 一个应用",pre/release/hotfix 走同一 release_key,
    # 通过 body.stage 分派 env/platform/github.env 等 stage-specific 字段
    stages = rel.get("stages") or {}
    stage_override = stages.get(req.stage) if req.stage else None
    if stage_override:
        rel = _deep_merge(rel, stage_override)

    # check-prereq：release 阶段必须是「同号 pre」的 promote（同一 commit 走两道门）。
    # 放在建审批实例【之前】——不合契约的发版根本不该惊动审批人。
    # fail-close：校验本身失败(GitHub 不可达/App 无权限)也不放行。理由同 release.yml 里
    # "approvo 不可达就 fail、绝不 || true 绕过"——门禁失灵时默认拒绝，而不是默认放行。
    if (req.stage or "") == "release":
        try:
            _ok, _why = github.check_release_promotes_pre(rel.get("source_repo") or req.repo, req.tag)
        except Exception as e:  # 宽捕获是有意的(BLE001 已在 ruff.toml 全局 ignore)
            raise HTTPException(502, f"check-prereq 无法校验(fail-close,不放行): {type(e).__name__}: {e}")
        if not _ok:
            raise HTTPException(409, f"check-prereq 未通过：{_why}")

    image = f"{rel['image_repo']}:{req.tag}" if rel.get("image_repo") else req.tag
    method = rel.get("method", "kubectl")

    g = rel.get("github") or {}

    # 解析申请人:github 用户名 → 映射表(取 open_id + 真名)> 显式 user_id > 邮箱反查 > 兜底
    row = store.usermap_get_row(req.github_actor) if req.github_actor else None
    initiator = req.operator_user_id or (row["user_id"] if row else None)
    if not initiator and req.operator_email:
        initiator = feishu.user_id_by_email(req.operator_email)
    initiator = initiator or settings.DEFAULT_INITIATOR
    if not initiator:
        raise HTTPException(400, "无法确定申请人(在 /admin 配 github 映射,或设 default_initiator_user_id)")

    # 名字优先级: 映射真名 > 邮箱 > GitHub 名 > 兜底
    mapped_name = (row and row.get("name")) or ""
    operator_name = mapped_name or req.operator_email or req.github_actor or "-"

    platform = rel.get("platform", "-")              # 部署目标:海外 / 国内
    env = req.env or rel.get("env") or g.get("env")  # 请求覆盖 > release 配置 > github 配置
    now = datetime.now(settings.CN_TZ).strftime("%Y-%m-%d %H:%M")

    # 本次 commit:CI 显式传的优先,没传就从镜像 tag 末段抽短 sha(部分 CI 不传 commit)
    m = store.TAG_SHA_RE.search(req.tag or "")
    head_commit = req.commit or (m.group(1) if m else "")

    project = req.project or rel.get("project") or settings.DEFAULT_PROJECT or None
    spec = {
        "repo": req.repo, "tag": req.tag, "image": image,
        "stage": req.stage,                          # pre/release/hotfix
        "project": project,                          # 卡片标题前缀
        "method": method, "platform": platform, "env": env,
        "cluster": rel.get("cluster"), "namespace": rel.get("namespace"),
        "deployment": rel.get("deployment"), "container": rel.get("container"),
        "helm_release": rel.get("helm_release"), "chart": rel.get("chart"),
        "image_key": rel.get("image_key", "image.tag"),
        "github": g or None,
        "operator_name": operator_name,
        "operator_github": req.github_actor,   # 原始 GitHub 用户名 (submit_card 两行显示用)
        "operator_mapped": bool(mapped_name),  # True=真名可用,False=只有 github/email
        "operator_id": initiator,        # @ 用 user_id(全租户通用)
        "submit_time": now, "commit": head_commit,
        # notes：申请方随请求传来的自由文本。清单应用类申请把 kubectl diff 放这里，
        # 让审批人看得见"集群会发生什么"——PR 清单说明不了这件事。
        # ⚠️ 此前只把 notes 放进了 _build_form(飞书「审批」表单)，卡片取的是 spec，
        # 于是卡片上什么都没有。传进去 ≠ 看得见，两个通道要分别喂。
        "notes": req.notes or "",
        "changes": _collect_changes(rel, req, head_commit),
    }

    # 审批定义表单复用现有 8 控件(github 方式把 owner/repo、env 塞进集群/命名空间位)
    if method == "github":
        cluster_val, ns_val = f"{g.get('owner')}/{g.get('repo')}", env or "prod"
    else:
        cluster_val, ns_val = rel.get("cluster", ""), rel.get("namespace", "")
    form_json = _build_form({
        "repo": req.repo, "tag": req.tag, "cluster": cluster_val,
        "namespace": ns_val, "image": image,
        "operator": operator_name, "commit": head_commit, "notes": req.notes or "",
    })

    # 防重与重发:进行中的不许重复提交;已部署成功的幂等跳过(重跑 workflow 不再走审批);
    # 被拒/撤销/部署失败的允许重新申请——uuid 带上历史次数,避开飞书同 uuid 防重(60012)
    history = store.status_history(req.repo, req.tag)
    if any(s in ("pending", "deploying") for s in history):
        raise HTTPException(409, f"{req.repo}:{req.tag} 已有进行中的审批或部署,勿重复提交")
    if history and history[0] == "success":
        return {"skipped": "already deployed", "repo": req.repo, "tag": req.tag, "image": image}
    uuid = hashlib.sha256(f"{req.repo}:{req.tag}:{req.commit}:{len(history)}".encode()).hexdigest()[:32]

    # 动态审批人 = 审批群当前成员(或签,任一通过即可)。不在群里=不是审批人=批不了。
    # 审批定义的审批节点是「自选/Free」,审批人在这里实时传入,跟随群成员增减。
    approvers = feishu.get_chat_members(settings.DETAIL_CHAT_ID)
    if not approvers:
        raise HTTPException(500, "审批群无有效成员,无法指派审批人(检查机器人是否在群、是否有读取群成员权限)")
    node_approvers = [{"key": "APPROVE", "value": approvers}]

    # 按 stage 选审批定义(pre 可能用动态群成员或签,release 可能固定 CEO)
    approval_code = settings.approval_code_for(req.stage)
    # 若审批定义是固定审批人(非 Free 节点), 传 node_approver_user_id_list 会被忽略, 不会报错
    instance_code = feishu.create_instance(approval_code, initiator, form_json,
                                           uuid=uuid, node_approver_user_id_list=node_approvers)
    store.save(instance_code, spec)
    feishu.send_card(settings.DETAIL_CHAT_ID, cards.submit_card(spec))   # 详细卡 → 审批群
    return {"instance_code": instance_code, "repo": req.repo, "tag": req.tag, "image": image}


# ---------- 申请人映射管理(/admin 内置页 + API,Basic Auth)----------

class UserMapReq(BaseModel):
    github_login: str
    user_id: str
    name: str | None = ""


@app.get("/api/usermap")
def api_usermap_list(_: str = Depends(require_admin)):
    return store.usermap_list()


@app.post("/api/usermap")
def api_usermap_set(req: UserMapReq, _: str = Depends(require_admin)):
    gh, uid = req.github_login.strip(), req.user_id.strip()
    if not gh or not uid:
        raise HTTPException(400, "github_login 和 user_id 必填")
    store.usermap_set(gh, uid, (req.name or "").strip())
    return {"ok": True}


@app.delete("/api/usermap/{github_login}")
def api_usermap_delete(github_login: str, _: str = Depends(require_admin)):
    return {"ok": store.usermap_delete(github_login)}


@app.get("/api/resolve")
def api_resolve(email: str, _: str = Depends(require_admin)):
    """邮箱反查飞书 user_id,方便填表。"""
    return {"email": email, "user_id": feishu.user_id_by_email(email)}


@app.get("/admin", response_class=HTMLResponse)
def admin_page(_: str = Depends(require_admin)):
    return ADMIN_HTML


ADMIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>发版申请人映射</title>
<style>
 body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:760px;margin:32px auto;padding:0 16px;color:#222}
 h1{font-size:20px} h2{font-size:15px;margin-top:28px}
 .hint{color:#666;font-size:13px;line-height:1.6;background:#f6f8fa;padding:10px 12px;border-radius:8px}
 table{border-collapse:collapse;width:100%;margin-top:12px;font-size:14px}
 th,td{border:1px solid #e3e6ea;padding:7px 10px;text-align:left}
 th{background:#f6f8fa} td button{font-size:12px;padding:2px 8px}
 .form{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
 input{padding:6px 9px;border:1px solid #ccd0d5;border-radius:6px;font-size:14px}
 button{cursor:pointer;border:1px solid #ccd0d5;background:#fff;border-radius:6px;padding:6px 12px}
 button.p{background:#2b6cb0;color:#fff;border-color:#2b6cb0}
 #rz{align-self:center;color:#666;font-size:13px}
</style></head><body>
<h1>GitHub 账号 → 飞书申请人 映射</h1>
<p class="hint">维护「谁推 tag = 哪个飞书用户」,只影响审批卡片上的<b>申请人归属</b>,不是发版权限。<br>
离职:在 GitHub 踢人即可(推不了 tag 就发不起);这张表清不清都行。未登记的人也能发版,只是标注「未映射」。</p>
<table><thead><tr><th>GitHub 用户名</th><th>姓名</th><th>飞书 user_id</th><th>更新时间</th><th>操作</th></tr></thead><tbody id="tb"></tbody></table>
<h2>新增 / 更新</h2>
<div class="form">
 <input id="gh" placeholder="GitHub 用户名">
 <input id="nm" placeholder="姓名(可空)">
 <input id="uid" placeholder="飞书 user_id">
 <button class="p" onclick="save()">保存</button>
</div>
<div class="form">
 <input id="email" placeholder="不知道 user_id?用邮箱反查">
 <button onclick="resolve()">解析</button><span id="rz"></span>
</div>
<script>
async function load(){
 const rows=await (await fetch('/api/usermap')).json();
 tb.innerHTML='';
 for(const x of rows){
  const tr=document.createElement('tr');
  for(const v of [x.github_login, x.name||'', x.user_id, x.updated_at||'']){
   const td=document.createElement('td'); td.textContent=v; tr.appendChild(td);  // textContent 防 XSS
  }
  const td=document.createElement('td');
  const be=document.createElement('button'); be.textContent='编辑';
  be.addEventListener('click',()=>{gh.value=x.github_login;nm.value=x.name||'';uid.value=x.user_id;});
  const bd=document.createElement('button'); bd.textContent='删除'; bd.style.marginLeft='6px';
  bd.addEventListener('click',()=>del(x.github_login));
  td.appendChild(be); td.appendChild(bd); tr.appendChild(td);
  tb.appendChild(tr);
 }
}
async function save(){
 if(!gh.value||!uid.value){alert('GitHub 用户名和 user_id 必填');return;}
 await fetch('/api/usermap',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({github_login:gh.value,user_id:uid.value,name:nm.value})});
 gh.value=nm.value=uid.value=''; load();
}
async function del(g){ if(!confirm('删除 '+g+' ?'))return;
 await fetch('/api/usermap/'+encodeURIComponent(g),{method:'DELETE'}); load();}
async function resolve(){
 rz.textContent='查询中...';
 const d=await (await fetch('/api/resolve?email='+encodeURIComponent(email.value))).json();
 rz.textContent=d.user_id?('→ '+d.user_id+'(已填入)'):'未找到'; if(d.user_id) uid.value=d.user_id;
}
load();
</script></body></html>"""


class ViewerTokenReq(BaseModel):
    user_id: str                       # 飞书 user_id —— 身份，不是共享 token
    reason: str = ""                   # 事由，会进群卡片与审计
    cluster: str = "prod-cluster"
    namespace: str = "sc"
    minutes: int = 30


@app.post("/viewer-token")
def viewer_token(req: ViewerTokenReq, x_release_token: str = Header(default="")):
    """签发短期只读 kubeconfig，让「查日志/看状态」不必 ssh。

    🔴 为什么【不走审批】却仍然安全：
    审批解决的是"该不该做"，这里真正要解决的是"你是谁"——
    共享的 RELEASE_TOKEN 谁都能拿到(org secret / qa16 上 root 可读)，
    它【无法归因】，所以只有它是不够的。故：
      ① 必须提供飞书 user_id，且该人【确实在审批群里】(不在群=不是内部人)
      ② 每次签发立即往群里发卡 —— 可归因 + 全群可见，异常签发当场被发现
    只读 + 30 分钟 + 全群可见，比"批一次然后没人记得"更有威慑，且不会因为
    "查个日志还要等审批"把人逼回去 ssh。
    """
    if not _rate_ok():
        raise HTTPException(429, "too many requests")
    if settings.RELEASE_TOKEN and not secrets.compare_digest(x_release_token, settings.RELEASE_TOKEN):
        raise HTTPException(401, "bad release token")
    if not 1 <= req.minutes <= 120:
        raise HTTPException(400, "minutes 需在 1~120 之间(只读凭证不应长期有效)")

    # 🔴 目标白名单：本接口此前只校验"你是谁"(token + 群成员)，从不校验"你能操作哪个目标"，
    # 而 cluster/namespace 是 body 里任意可填的 —— 同集群还跑着别的团队的生产 namespace(受限环境 生产)。
    # 与卡片按钮共用同一份判据：同一能力的两个入口若只加固一条，绕过者会走另一条。
    # ⚠️ 这是 fail-close：config 里【必须】配 viewer_targets，否则本接口一律拒绝。
    #    升级时要先更新 config 再更新镜像，别把正常路径打断。
    if not k8s.target_allowed(req.cluster, req.namespace):
        raise HTTPException(
            403, f"目标 {req.cluster}/{req.namespace} 不在 viewer_targets 白名单内"
                 f"(若为新目标，请先在 config.yaml 的 viewer_targets 中登记)")

    members = feishu.get_chat_members(settings.DETAIL_CHAT_ID)
    if not members:
        raise HTTPException(500, "读不到审批群成员，无法校验身份")
    if req.user_id not in members:
        raise HTTPException(403, "申请人不在审批群中，拒绝签发")

    ok, out = k8s.issue_viewer_kubeconfig(req.cluster, req.namespace, req.minutes)
    if not ok:
        raise HTTPException(502, out)

    name = store.name_by_id(req.user_id) or req.user_id
    now = datetime.now(settings.CN_TZ).strftime("%Y-%m-%d %H:%M")
    # 通知失败绝不能阻断签发本身(同 _run_deploy 的教训)，但要留痕
    try:
        feishu.send_card(settings.RESULT_CHAT_ID, cards.viewer_token_card(
            name=name, cluster=req.cluster, namespace=req.namespace,
            minutes=req.minutes, reason=req.reason, when=now))
    except Exception as e:  # 通知是辅助，不决定主流程
        print(f"[viewer-token][warn] 通知卡片发送失败(不影响签发): {type(e).__name__}: {e}")
    print(f"[viewer-token] {name}({req.user_id}) {req.cluster}/{req.namespace} "
          f"{req.minutes}m reason={req.reason!r}")
    return {"kubeconfig": out, "expires_in_minutes": req.minutes,
            "cluster": req.cluster, "namespace": req.namespace}


class ReleaseKeyReq(BaseModel):
    grant: str


@app.post("/release-key")
def release_key(req: ReleaseKeyReq):
    """用一次性 grant 兑换 RELEASE_TOKEN。

    🔴 本端点【刻意不校验 RELEASE_TOKEN】—— 它就是用来取那个 token 的，
    要求先有 token 才能取 token 是循环依赖。安全性完全落在 grant 上：
      · 256 位随机，暴力不可行
      · 10 分钟有效 + 一次性（兑换即销毁）
      · 只能由【审批群成员】从 Lark 菜单取得 → 可归因
      · 签发时已在群内公示 → 异常申请当场可见

    ⚖️ 为什么要有它：RELEASE_TOKEN 有权威存储（k8s Secret）却【没有受控取用路径】，
    2026-08-10 两次为了给新仓配 secret 而动用集群超管 —— 用超管取一个共享 token，
    权限差了几个数量级，还逼着人把超管常备手边，与"消灭常驻高权限"完全相反。
    """
    if not _rate_ok():
        raise HTTPException(429, "too many requests")
    ok, name, err = keygrant.redeem(req.grant)
    if not ok:
        # 不区分"不存在/已用/过期"以外的细节，避免给探测者额外信息
        raise HTTPException(401, err)
    if not settings.RELEASE_TOKEN:
        raise HTTPException(500, "服务端未配置 RELEASE_TOKEN")
    # 🔴 只记谁取的，绝不记 token 本身
    print(f"[release-key] {name} 兑换成功（凭证已销毁）")
    return {"release_token": settings.RELEASE_TOKEN,
            "gate_url": settings.PUBLIC_GATE_URL,
            "hint": "配置 GitHub secret 时请用管道传值，勿粘贴进聊天/文档"}


@app.post("/viewer-panel")
def viewer_panel(_: str = Depends(require_admin)):
    """往审批群发一张常驻的「只读凭证自助签发」面板卡（点按钮 → 机器人私聊发 kubeconfig）。

    🔴 为什么用 admin 认证而不是 RELEASE_TOKEN：这是【管理动作】——它决定群里出现哪些
    按钮、按钮指向哪个集群/namespace。若用共享的 RELEASE_TOKEN，任何能发版的 CI
    都能往群里投一张"看起来正规"的卡片，把人骗去点。发卡权必须比用卡权更严。
    """
    targets = settings.VIEWER_TARGETS
    if not targets:
        # 🔴 不配就报错，而不是发一张没有按钮的空卡（空卡看起来像成功）
        raise HTTPException(400, "未配置 viewer_targets，拒绝发空面板")
    if not settings.DETAIL_CHAT_ID:
        raise HTTPException(500, "未配置审批群 chat_id，无处可发")
    feishu.send_card(settings.DETAIL_CHAT_ID, cards.viewer_panel_card(targets))
    print(f"[viewer-panel] 面板已发到审批群，按钮: {[t.get('label') for t in targets]}")
    return {"ok": True, "buttons": [t.get("label") for t in targets]}


# ─────────────────── 两步式 Secret 变更 ───────────────────
# 为什么两步：审批是异步的，若值随申请一起提交，approvo 就必须把它【存住等人来批】，
# 而 spec 会序列化进 release.db（PVC 上，任何能 exec 进 approvo 的人都读得到）。
# 两步式让值【全程不落库】：
#   ① /secret/request  申请"允许改 Secret X 的 key Y" → 走正常审批（值不参与）
#   ② /secret/commit   审批通过后单独提交值 → 直接写 k8s，只在内存中过一遍
# 反正审批人本来也看不到值，批的实质就是"允许改这个 key"。
SECRET_GRANT_TTL_MIN = 30      # 授权有效期：批准后多久内必须使用
_SECRET_RELEASE_KEY = "sc-secret"


class SecretRequestReq(BaseModel):
    user_id: str                        # 飞书身份（共享 token 无法归因，见 /viewer-token）
    name: str                           # Secret 名
    key: str                            # 要改的 key
    reason: str = ""
    cluster: str = "prod-cluster"
    namespace: str = "sc"


class SecretCommitReq(BaseModel):
    request_id: str                     # /secret/request 返回的 instance_code
    value: str                          # 🔴 只在内存中过一遍，不落库不进日志


@app.post("/secret/request")
def secret_request(req: SecretRequestReq, x_release_token: str = Header(default="")):
    """第一步：申请「允许改某个 Secret 的某个 key」。值不在此提交。"""
    if not _rate_ok():
        raise HTTPException(429, "too many requests")
    if settings.RELEASE_TOKEN and not secrets.compare_digest(x_release_token, settings.RELEASE_TOKEN):
        raise HTTPException(401, "bad release token")
    members = feishu.get_chat_members(settings.DETAIL_CHAT_ID)
    if not members or req.user_id not in members:
        raise HTTPException(403, "申请人不在审批群中，拒绝受理")

    # 区分「新增 key」与「覆盖已有 key」——覆盖是破坏性的(旧值不可恢复)，
    # 审批人必须知道自己批的是哪一种。查不到就明说查不到，不猜。
    ok, exists, err = k8s.secret_key_exists(req.cluster, req.namespace, req.name, req.key)
    if not ok:
        raise HTTPException(502, f"读取 Secret 失败（无法判断是新增还是覆盖）：{err}")
    action = "覆盖已有 key（旧值不可恢复）" if exists else "新增 key"

    rel = dict(settings.RELEASES.get(_SECRET_RELEASE_KEY) or {})
    if not rel:
        raise HTTPException(404, f"config.releases 未配置 '{_SECRET_RELEASE_KEY}'")
    tag = f"{req.namespace}/{req.name}#{req.key}@{datetime.now(settings.CN_TZ):%Y%m%d-%H%M%S}"
    notes = (f"【Secret 变更授权申请】\n"
             f"目标: {req.cluster} {req.namespace}/{req.name}\n"
             f"key : {req.key}\n"
             f"动作: {action}\n"
             f"事由: {req.reason or '(未填写)'}\n\n"
             f"⚠️ 本次审批【不包含值】。批准后申请方须在 {SECRET_GRANT_TTL_MIN} 分钟内"
             f"单独提交值，逾期或用过一次即失效。值不落库、不入 git、不进日志。")
    # 直接复用 /release 的完整逻辑（建审批实例、发卡、落库、防重），不另造一套
    return release(ReleaseReq(repo=_SECRET_RELEASE_KEY, tag=tag, stage="release",
                              operator_user_id=req.user_id, notes=notes),
                   x_release_token=x_release_token)


@app.post("/secret/commit")
def secret_commit(req: SecretCommitReq, x_release_token: str = Header(default="")):
    """第二步：授权生效后提交值，直接写入集群。值只在内存中过一遍。"""
    if settings.RELEASE_TOKEN and not secrets.compare_digest(x_release_token, settings.RELEASE_TOKEN):
        raise HTTPException(401, "bad release token")
    rec = store.get(req.request_id)
    if not rec or rec["repo"] != _SECRET_RELEASE_KEY:
        raise HTTPException(404, "授权不存在")
    # 一次性：success = 已授权待用；用过后置为 used，再来一次就落到这里被拒
    if rec["status"] != "success":
        raise HTTPException(409, f"授权状态为 {rec['status']}，不可用"
                                 "（未批准 / 已使用 / 已拒绝）")
    spec = json.loads(rec["spec_json"])
    approved_at = datetime.fromisoformat(rec["updated_at"])
    if (datetime.now() - approved_at).total_seconds() > SECRET_GRANT_TTL_MIN * 60:
        store.set_status(req.request_id, "expired")
        raise HTTPException(409, f"授权已超过 {SECRET_GRANT_TTL_MIN} 分钟有效期，请重新申请")
    if not req.value:
        raise HTTPException(400, "值不能为空")

    ns, rest = spec["tag"].split("/", 1)
    name, rest2 = rest.split("#", 1)
    key = rest2.split("@", 1)[0]
    ok, out = k8s.patch_secret(spec.get("cluster") or "prod-cluster", ns, name, key, req.value)
    if not ok:
        raise HTTPException(502, f"写入失败：{out}")
    store.set_status(req.request_id, "used")

    # 审计记【指纹】不记值：事后能核对"改成了哪一版"，但泄露不了内容
    fp = hashlib.sha256(req.value.encode()).hexdigest()[:12]
    print(f"[secret] {ns}/{name}#{key} 已更新 by request={req.request_id} sha256={fp}")
    try:
        feishu.send_card(settings.RESULT_CHAT_ID, cards.secret_done_card(
            ns=ns, name=name, key=key, fingerprint=fp,
            when=datetime.now(settings.CN_TZ).strftime("%Y-%m-%d %H:%M")))
    except Exception as e:
        print(f"[secret][warn] 通知失败(不影响写入): {type(e).__name__}: {e}")
    return {"ok": True, "target": f"{ns}/{name}#{key}", "value_sha256_12": fp,
            "note": "如引用该 Secret 的服务需要重启才生效，请另走 sc-config-change 通道"}
