"""RELEASE_TOKEN 的受控取用通道 —— 一次性、短时效的临时凭证。

🔴 为什么需要它：
RELEASE_TOKEN 有权威存储（k8s Secret `approvo-secrets`），但【没有受控的取用路径】。
2026-08-10 两次为了给新仓配 RELEASE_GATE_TOKEN 而动用集群【超管】——
用超管去取一个共享 token，权限差了好几个数量级，
而且逼着人把超管常备在手边，与「消灭常驻高权限」的整体方向正好相反。

⚖️ 更根本的教训：那天把「用完即毁」这条原则【用错了地方】——
短期凭证（超管 kubeconfig、SA token）用完即毁是对的；
【长期共享密钥】用完即毁 = 没有权威源可取，只会逼出更高的权限。

设计（与 Secret 两步式同一模式）：
  ① 飞书卡片点按钮 → 校验点击者确实在审批群 → 签发一次性 grant（默认 10 分钟）
     → 私聊发给【本人】→ 同时在群里公示（可归因）
  ② 用 grant 调 POST /release-key 换取 RELEASE_TOKEN，换完【立即失效】

🔴 为什么私聊发的是 grant 而不是 token 本身：
飞书聊天记录【永久留存】。直接发 token 等于把长期密钥永久写进聊天记录；
发一次性短时效 grant，则记录日后被翻出来也已无用。

🔴 为什么放内存不落库：
grant 生命周期只有 10 分钟，而落库会让它进入 PVC/RDS 并被备份带走 ——
一个短命凭证不该有比自己更长的副本。approvo 重启即全部失效，这是【期望行为】。
"""
import secrets
import threading
import time

TTL_MIN = 10

_lock = threading.Lock()
_grants: dict[str, dict] = {}


def _purge_locked(now: float) -> None:
    for k in [k for k, v in _grants.items() if v["exp"] < now]:
        _grants.pop(k, None)


def issue(user_id: str, name: str, ttl_min: int = TTL_MIN) -> tuple[str, int]:
    """签发一次性 grant。返回 (grant, 有效分钟数)。"""
    g = secrets.token_urlsafe(32)          # 256 位随机，足以单独作为凭证
    now = time.time()
    with _lock:
        _purge_locked(now)
        _grants[g] = {"user_id": user_id, "name": name,
                      "exp": now + ttl_min * 60, "used": False}
    return g, ttl_min


def redeem(grant: str) -> tuple[bool, str, str]:
    """兑换 grant。返回 (ok, 申请人名, 错误原因)。

    🔴 一次性：成功兑换后立即删除。并发下也只有一个能成功（整段在锁内）。
    """
    now = time.time()
    with _lock:
        _purge_locked(now)
        rec = _grants.get(grant)
        if not rec:
            return False, "", "凭证无效或已过期"
        if rec["used"]:
            return False, "", "凭证已被使用过（一次性）"
        if rec["exp"] < now:
            _grants.pop(grant, None)
            return False, "", "凭证已过期"
        _grants.pop(grant, None)           # 用完即删，不留痕
        return True, rec["name"], ""


def pending_count() -> int:
    with _lock:
        _purge_locked(time.time())
        return len(_grants)
