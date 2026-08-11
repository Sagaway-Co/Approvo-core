"""单独测部署执行器(不经飞书),验证 kubectl/helm 凭证和目标对不对。

  CONFIG_PATH=config.yaml python -m scripts.test_deploy <repo> <tag>
例:
  python -m scripts.test_deploy demo v1.2.3      # dryrun,安全
  python -m scripts.test_deploy api v1.2.3       # 真的会 set image,小心
"""
import sys

from app import deployer, settings


def main(repo, tag):
    rel = settings.RELEASES.get(repo)
    if not rel:
        print(f"repo '{repo}' 未在 config.releases 配置")
        sys.exit(1)
    image = f"{rel['image_repo']}:{tag}" if rel.get("image_repo") else tag
    method = rel.get("method", "kubectl")
    g = rel.get("github") or {}
    target = (f"{g.get('owner')}/{g.get('repo')} ({g.get('env', 'prod')})"
              if method == "github" else f"{rel.get('cluster', '-')}/{rel.get('namespace', '-')}")
    spec = {
        "repo": repo, "tag": tag, "image": image,
        "method": method, "target": target, "github": g or None,
        "cluster": rel.get("cluster"), "namespace": rel.get("namespace"),
        "deployment": rel.get("deployment"), "container": rel.get("container"),
        "helm_release": rel.get("helm_release"), "chart": rel.get("chart"),
        "image_key": rel.get("image_key", "image.tag"),
    }
    print(f"[test] method={spec['method']} -> {image}")
    ok, log = deployer.deploy(spec)
    print("----- 部署日志 -----")
    print(log)
    print(f"----- 结果: {'成功' if ok else '失败'} -----")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
