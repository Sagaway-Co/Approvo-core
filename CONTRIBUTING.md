# Contributing to approvo
> ## 🚨 本仓库对公众开放
>
> 提交任何内容前，请确认不含：**凭据**（口令/token/AccessKey/证书/私钥/kubeconfig）、
> **网络与拓扑**（内网 IP/内网域名/集群名/namespace/镜像仓库实例地址）、
> **身份**（真实人名/手机号/IM 平台的 app_id·app_secret·open_id·chat_id）、
> **组织信息**（客户名/内部仓库名/内部服务清单）。
>
> 举例请用中性占位符（`my-app` / `registry.example.com` / `https://approvo.example.com`）。
> CI 的 `leak-guard` 会做模式匹配兜底，但**它拦不住语义上的敏感信息** —— 那部分靠人。
> 详见 [CLAUDE.md](CLAUDE.md)（同样适用于人类）。

感谢关注 approvo!本项目脱胎于生产在用的内部工具,现在开源;欢迎 PR、Issue、想法.

## 提 Issue

- **Bug**:请附最小复现步骤 + approvo 版本(镜像 tag / helm chart version) + 相关日志(注意脱敏 App Secret 等凭证)
- **Feature**:先说使用场景再说方案;approvo 保持"轻量单副本"定位,涉及架构层面(比如多副本 HA、Postgres backend)的提案请先在 Discussions 讨论

## 提 PR

1. Fork 或建 feature branch:`feature/<short-name>`
2. 遵循代码风格:
   - Python:`ruff check .` 通过
   - 类型标注尽量补,新增模块必补(见 [PROVIDERS.md](docs/PROVIDERS.md) 参考)
   - 提交信息用中文或英文均可,推荐 [Conventional Commits](https://www.conventionalcommits.org/) (`feat:` / `fix:` / `refactor:` / `docs:` / `chore:`)
3. **`grep -riE "<legacy-identifier>|<legacy-identifier>|<legacy-identifier>|<legacy-identifier>"` 必须干净**(CI 里有 gate 会挡)
4. 测试:如果动了 store / provider / deployer 请补对应 pytest;文档改动无需测试
5. 一个 PR 一件事,别打混合大补丁

## 增加 Provider / Deployer / Source

见分模块的贡献指南:
- 新 approval provider(飞书/钉钉/Slack/企微 ...):[docs/PROVIDERS.md](docs/PROVIDERS.md)
- 新 deployer(argocd / ssh / ...):[docs/DEPLOYERS.md](docs/DEPLOYERS.md)

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install ruff mypy pytest
cp config.example.yaml config.yaml   # 改 approval_code / chat_id 等
export FEISHU_APP_ID=... FEISHU_APP_SECRET=... RELEASE_TOKEN=$(openssl rand -hex 16)
python -m app.main
```

见 [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) 完整上手.

## 行为准则

无 CoC 独立文件,但请遵循基本 open source 礼节:友善、无歧视、以事论事.对同一话题反复恶意 comment 会被 block.

## 许可证

贡献代码即表示你同意以 [Apache-2.0](LICENSE) 协议提供给项目.你保留自己的版权.
