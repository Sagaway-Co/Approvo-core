# Security Policy

## 支持版本

| 版本      | 支持状态 |
| -------- | ------ |
| 0.1.x    | ✓      |
| < 0.1.0  | ✗      |

发布 v0.2 后 0.1.x 转 security-only,再一版后终止支持.

## 报告漏洞

**不要在公开 issue 里贴漏洞细节.** 请通过 GitHub Security Advisories 私密报告:

https://github.com/Sagaway-Co/Approvo-core/security/advisories/new

报告请包含:
- 影响范围 (哪个版本 / 部署形态)
- 复现步骤 (最小可执行示例最佳)
- 建议修复思路 (可选)

响应节奏:
- 48 小时内确认收到
- 高危 (RCE / 越权直接读凭证) 14 天内出 patch + advisory
- 中危 / 低危按季度节奏纳入下一个 minor release

## 已知安全边界

见 [docs/SECURITY.md](docs/SECURITY.md) 完整威胁模型:
- 凭证隔离设计 (approvo 不持 kubeconfig,只调 GitHub Actions)
- 公网入口结构性隔离 (只暴露 `/release` + `/healthz`,`/admin` 走内网)
- SQLite 单副本 (不支持横向扩容;单节点故障即断服)
- 长连接依赖出方向到 `open.feishu.cn:443`
