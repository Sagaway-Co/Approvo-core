"""部署凭据的动态签发 —— 取代常驻 runner 磁盘的长期 kubeconfig。

## 为什么需要
CI 部署到 k8s 的常规做法是把一份 kubeconfig 放在 runner 上。它的问题是:
  · 那份凭据往往是【长期有效】的(甚至不过期),且常驻磁盘;
  · 权限往往远超"部署这一个 namespace"(轻量发行版的默认 kubeconfig 就是全集群超管);
  · 任何能在 runner 上执行命令的人(或任何被注入的 workflow)都直接拿到它。
即"要不要部署"这件事有审批把关,而"能不能部署"这件事却一直敞着。

## 机制(每次部署一副凭据,用完即毁)
  ① approvo 派发部署时,签发一次性 `grant`(256 位随机、10 分钟、内存态)
     作为 workflow 输入传给流水线。grant 本身【不是集群凭据】,
     还必须配合 RELEASE_TOKEN 才能兑换 —— 所以它落在 run 元数据里也无用。
  ② 流水线用 grant 兑换:approvo 建一个一次性 Secret 作为 boundObjectRef,
     再签一枚 10 分钟、限定该 ns 的 SA token,返回 kubeconfig 文本。
  ③ 部署结束(无论成败)流水线回调撤销 → approvo 删掉那个 Secret → token 当场失效。

## 实测数据(决定了上面的设计,别"优化"掉)
  · TTL 下限是 10 分钟:请求 1m / 5m 会被 API server 【直接拒绝】,不是静默抬高。
  · 删 boundObjectRef 后:0 秒仍可用 → 15 秒起 Unauthorized(API server 认证缓存约 10s)。
  · 删 SA 则 3 秒内失效(留作紧急撤销;会影响并发部署,不作常规手段)。
  ⇒ 所以暴露窗口 = 部署时长 + ~15 秒,而不是 TTL 的 10 分钟。

## 为什么内存态、不落库(与 keygrant 同一取舍)
grant 只活 10 分钟,落库会让它进数据库并被备份带走 ——
一个短命凭证不该有比自己更长的副本。approvo 重启即全部失效,这是【期望行为】:
重启后正在跑的部署会兑换失败而中止,这比留一把可复用的钥匙好。

## 槽位为什么是固定名池
k8s 的 `create` 【无法】按资源名限制,但 `delete` 可以。若给 approvo 无名的
secrets delete 权限,它就有权删掉那个 namespace 里的真机密(数据库口令之类)。
故固定 8 个槽位名,RBAC 里 delete 用 resourceNames 限死这 8 个;approvo 每次挑空槽。
槽满 = 拒绝签发(fail-close),绝不退化成"不绑定就签"。
"""
import os
import secrets
import threading
import time


def _ttl_from_env() -> int:
    """DEPLOY_CRED_TTL_MIN:部署凭据有效分钟数,默认 10。

    🔴 只接受 10..60:TokenRequest 下限 10 分钟(更短会被 API server 拒绝);上限防止误配成长期凭据。
    取值非法【拒绝启动】,绝不悄悄回落到某个默认值。部署结束仍会立即吊销,这里只是上限。
    """
    raw = os.environ.get("DEPLOY_CRED_TTL_MIN", "10").strip()
    if not raw.isdigit() or not 10 <= int(raw) <= 60:
        raise RuntimeError(f"DEPLOY_CRED_TTL_MIN 必须是 10..60 的整数,当前 {raw!r}")
    return int(raw)


TTL_MIN = _ttl_from_env()        # 默认与 API server 的 TokenRequest 下限一致
SLOTS = 8                         # 与 RBAC resourceNames 里的槽位数一一对应
SLOT_FMT = "approvo-deploy-cred-{}"

_lock = threading.Lock()
_grants: dict[str, dict] = {}
_slots_in_use: dict[str, str] = {}    # slot 名 -> grant 指纹(仅用于占位判断)


def _purge_locked(now: float) -> None:
    for k in [k for k, v in _grants.items() if v["exp"] < now]:
        _grants.pop(k, None)


def issue_grant(instance_code: str, cluster: str, namespace: str,
                ttl_min: int = TTL_MIN, approved_by: str = "") -> tuple[str, int]:
    """签发一次性兑换券。返回 (grant, 有效分钟)。approved_by 随兑换结果回给流水线,用于留痕。"""
    g = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        _purge_locked(now)
        _grants[g] = {"instance_code": instance_code, "cluster": cluster,
                      "namespace": namespace, "approved_by": approved_by or "",
                      "exp": now + ttl_min * 60}
    return g, ttl_min


def redeem_grant(grant: str) -> tuple[bool, dict, str]:
    """兑换。返回 (ok, {instance_code,cluster,namespace,approved_by}, 错误原因)。

    🔴 一次性:成功即删除,整段在锁内 —— 并发下只有一个能成功。
    """
    now = time.time()
    with _lock:
        _purge_locked(now)
        rec = _grants.get(grant)
        if not rec:
            return False, {}, "凭证无效或已过期"
        if rec["exp"] < now:
            _grants.pop(grant, None)
            return False, {}, "凭证已过期"
        _grants.pop(grant, None)
        return True, {k: rec.get(k, "") for k in ("instance_code", "cluster", "namespace", "approved_by")}, ""


def claim_slot() -> str | None:
    """占一个空槽位。槽满返回 None(调用方必须据此拒绝签发,不得跳过绑定)。"""
    with _lock:
        for i in range(1, SLOTS + 1):
            name = SLOT_FMT.format(i)
            if name not in _slots_in_use:
                _slots_in_use[name] = str(time.time())
                return name
    return None


def release_slot(name: str) -> None:
    with _lock:
        _slots_in_use.pop(name, None)


def slots_in_use() -> int:
    with _lock:
        return len(_slots_in_use)


def pending_count() -> int:
    with _lock:
        _purge_locked(time.time())
        return len(_grants)
