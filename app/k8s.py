"""集群只读凭证签发 —— 让「查日志/看状态」不必 ssh 进服务器。

为什么需要它：人登服务器【多数时候是为了看，不是为了改】。若查日志还得 ssh，
ssh 权限就永远收不回来；收不回来，改的能力也就还在。所以要先给"看"一条不经 shell 的路。

安全取向：
- 签发的是【短期(默认 30 分钟)、只读、namespace 受限】的 ServiceAccount token。
- 🔴 只读【不含 secrets】——只读也不该看到密钥值。
- 🔴 只读【不含 exec/portforward】——那是拿 shell，正是本通道要消灭的东西，
  不能混进"只读"里。需要 exec 是另一件事，必须单独审批。
- 身份不靠共享 token：调用方必须提供飞书 user_id 且【确实在审批群里】。
  共享的 RELEASE_TOKEN 无法归因是谁签的，不构成身份。
- 每次签发都往群里发卡：可归因 + 全群可见，异常签发立刻被发现。
"""
import subprocess

from app import settings

VIEWER_SA = "sc-viewer"


def target_allowed(cluster: str, namespace: str) -> bool:
    """签发只读凭证的目标白名单 —— 只允许 config 的 viewer_targets 里显式列出的组合。

    🔴 为什么两条入口都要用它:卡片按钮与 /viewer-token 是【同一个能力的两个入口】。
    只给新入口加白名单等于没加 —— 绕过者会走没加的那条。故下沉到这里共用。

    🔴 为什么不能信调用方给的 cluster/namespace:
    - 卡片的 action.value 是外部输入,任何能往群里发卡片的人都能构造指向别处的按钮;
    - /viewer-token 的 body 更是任意可填,而它只校验"你是谁"(RELEASE_TOKEN + 群成员),
      从不校验"你能操作哪个目标"。
    同集群还跑着别的团队的生产 namespace(另一团队的生产 namespace,红线是绝对不能碰),不能只靠下游 RBAC 兜底。
    """
    return any(str(t.get("cluster")) == cluster and str(t.get("namespace")) == namespace
               for t in settings.VIEWER_TARGETS)


def _kubectl(cluster: str, args: list[str], timeout: int = 20) -> tuple[bool, str]:
    cl = settings.CLUSTERS.get(cluster) or {}
    kubeconfig = cl.get("kubeconfig")
    if not kubeconfig:
        return False, f"cluster '{cluster}' 未在 config.clusters 配置 kubeconfig"
    env = {"KUBECONFIG": kubeconfig, "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"}
    if cl.get("context"):
        args = ["--context", cl["context"], *args]
    p = subprocess.run(["kubectl", *args], env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def issue_viewer_kubeconfig(cluster: str, namespace: str, minutes: int = 30) -> tuple[bool, str]:
    """签发只读 kubeconfig 文本。失败返回 (False, 错误说明)。"""
    ok, out = _kubectl(cluster, ["-n", namespace, "create", "token", VIEWER_SA,
                                 f"--duration={minutes}m"])
    if not ok:
        return False, f"签发 token 失败：{out.strip()[:300]}"
    token = out.strip()
    if not token:
        return False, "签发 token 失败：返回为空"

    ok, server = _kubectl(cluster, ["config", "view", "--raw", "--minify",
                                    "-o", "jsonpath={.clusters[0].cluster.server}"])
    ok2, ca = _kubectl(cluster, ["config", "view", "--raw", "--minify",
                                 "-o", "jsonpath={.clusters[0].cluster.certificate-authority-data}"])
    if not (ok and ok2 and server.strip()):
        return False, "读取集群地址/CA 失败"

    kubeconfig = f"""apiVersion: v1
kind: Config
clusters:
- name: {cluster}
  cluster:
    server: {server.strip()}
    certificate-authority-data: {ca.strip()}
contexts:
- name: viewer
  context: {{cluster: {cluster}, namespace: {namespace}, user: viewer}}
current-context: viewer
users:
- name: viewer
  user: {{token: {token}}}
"""
    return True, kubeconfig


def patch_secret(cluster: str, namespace: str, name: str, key: str,
                 value: str) -> tuple[bool, str]:
    """把 Secret 的某个 key 改成 value。

    🔴 值的生命周期：HTTP 请求 → 本进程内存 → kubectl stdin → k8s API。
    【绝不落库、绝不入 git、绝不进日志、绝不进命令行参数】。
    走 stdin 而不是 `kubectl create secret --from-literal=` 是刻意的 ——
    后者会把值写进进程 argv，同机任何人 `ps` 一下就看得见。
    """
    import base64
    import json

    b64 = base64.b64encode(value.encode()).decode()
    patch = json.dumps({"data": {key: b64}})
    cl = settings.CLUSTERS.get(cluster) or {}
    kubeconfig = cl.get("kubeconfig")
    if not kubeconfig:
        return False, f"cluster '{cluster}' 未配置 kubeconfig"
    env = {"KUBECONFIG": kubeconfig, "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"}
    args = ["kubectl", "-n", namespace, "patch", "secret", name,
            "--type=merge", "--patch-file=/dev/stdin"]
    if cl.get("context"):
        args[1:1] = ["--context", cl["context"]]
    p = subprocess.run(args, input=patch, env=env, capture_output=True, text=True, timeout=20)
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode == 0, out.strip()[:300]


def secret_key_exists(cluster: str, namespace: str, name: str, key: str) -> tuple[bool, bool, str]:
    """返回 (查询是否成功, key 是否已存在, 说明)。用于卡片上区分【新增 key】与【覆盖已有 key】——
    覆盖是破坏性的(旧值不可恢复)，审批人必须知道自己批的是哪一种。"""
    ok, out = _kubectl(cluster, ["-n", namespace, "get", "secret", name,
                                 "-o", "jsonpath={.data}"])
    if not ok:
        return False, False, out.strip()[:200]
    return True, (f'"{key}"' in out), ""
