"""ACK / EKS 部署执行器。支持 kubectl set image 和 helm upgrade --reuse-values。

集群凭证:config.yaml 的 clusters[name].kubeconfig 指向一个 kubeconfig 文件(挂进容器),
context 选 kubeconfig 里的上下文。镜像换好后等 rollout 完成再算成功。
"""
import os
import subprocess

from app import github, settings


def _run(cmd: list, env: dict, timeout: int):
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode == 0, out


def deploy(spec: dict):
    """返回 (ok: bool, log: str)。"""
    method = spec.get("method", "kubectl")

    # 推荐方式:审批通过 -> 触发 GitHub Actions 去部署,并等待 run 跑完拿真实结果
    if method == "github":
        return github.dispatch_and_wait(spec)

    # 测试用:不连任何集群,假装部署成功,用来跑通链路 / 看卡片
    if method == "dryrun":
        return True, (
            f"[dryrun] 不会真部署。\n"
            f"目标: {spec['cluster']}/{spec['namespace']} "
            f"deploy={spec.get('deployment') or spec.get('helm_release')}\n"
            f"镜像: {spec['image']}\n"
            f"$ (kubectl set image / helm upgrade 在这里被跳过)\n"
            f"deployment successfully rolled out  # 假的"
        )

    cl = settings.CLUSTERS.get(spec["cluster"])
    if not cl:
        return False, f"cluster {spec['cluster']} not in config"

    env = dict(os.environ)
    env["KUBECONFIG"] = cl["kubeconfig"]
    ctx = cl.get("context")
    ns = spec["namespace"]
    logs = []

    def log(cmd, out):
        logs.append("$ " + " ".join(cmd))
        logs.append(out.strip())

    if method == "kubectl":
        if spec.get("strategy") == "bluegreen":   # 蓝绿场景需单独处理,暂不接
            return False, "蓝绿部署暂未实现,已跳过"
        if not spec.get("deployment") or not spec.get("container"):
            return False, "kubectl 方式需要配置 deployment 和 container"
        ctx_args = (["--context", ctx] if ctx else [])
        dep = spec["deployment"]
        set_cmd = ["kubectl", *ctx_args, "-n", ns, "set", "image",
                   f"deployment/{dep}", f"{spec['container']}={spec['image']}"]
        ok, out = _run(set_cmd, env, 120)
        log(set_cmd, out)
        if not ok:
            return False, "\n".join(logs)
        roll_cmd = ["kubectl", *ctx_args, "-n", ns, "rollout", "status",
                    f"deployment/{dep}", "--timeout=300s"]
        ok, out = _run(roll_cmd, env, 360)
        log(roll_cmd, out)
        if not ok:
            # 滚动失败 -> 补抓原因:Pod 状态(ImagePullBackOff=拉不到镜像 / CrashLoop·readiness=健康检查不过)+ 事件
            pods = _run(["kubectl", *ctx_args, "-n", ns, "get", "pods"], env, 30)[1]
            pods = "\n".join(line for line in pods.splitlines()
                             if line.startswith("NAME") or line.startswith(dep + "-"))
            desc = _run(["kubectl", *ctx_args, "-n", ns, "describe", "deployment", dep], env, 30)[1]
            logs.append("--- 相关 Pod ---\n" + (pods or "(无)"))
            logs.append("--- Deployment 事件 ---\n" + "\n".join(desc.splitlines()[-15:]))
        return ok, "\n".join(logs)

    if method == "helm":
        if not spec.get("helm_release") or not spec.get("chart"):
            return False, "helm 方式需要配置 helm_release 和 chart"
        ctx_args = (["--kube-context", ctx] if ctx else [])
        cmd = ["helm", *ctx_args, "upgrade", spec["helm_release"], spec["chart"],
               "-n", ns, "--reuse-values",
               "--set", f"{spec.get('image_key', 'image.tag')}={spec['tag']}",
               "--wait", "--timeout", "5m"]
        ok, out = _run(cmd, env, 400)
        log(cmd, out)
        return ok, "\n".join(logs)

    return False, f"unknown deploy method: {method}"
