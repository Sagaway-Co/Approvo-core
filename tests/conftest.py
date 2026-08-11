"""pytest 前置：必须在 import app.* 之前把环境备齐。

🔴 app.settings 在【import 时】就读 os.environ["FEISHU_APP_ID"] 并打开 config 文件。
CI 里没有这些环境变量，若不在这里兜底，收集期就会 KeyError ——
而 pytest 的【收集期 ImportError 会连坐整个套件】，不是只跳过这一个文件。
"""
import os
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault("FEISHU_APP_ID", "cli_test")
os.environ.setdefault("FEISHU_APP_SECRET", "test-secret")
# 用仓内示例配置，绝对路径 —— 不依赖 pytest 从哪个 cwd 启动
os.environ.setdefault("CONFIG_PATH", str(_ROOT / "config.example.yaml"))
# 🔴 别让测试在仓根建 release.db（污染工作区、可能被误提交）。
# 也别用固定的 /tmp 文件名：共享 /tmp 上的固定名可能已被他人创建，
# 你的写入被拒而读取成功，于是静默用了别人的内容。
os.environ.setdefault(
    "DB_PATH", str(Path(tempfile.mkdtemp(prefix="approvo-test-")) / "release.db"))

# 状态库已迁 PostgreSQL。DB_DSN 不设也能 import（连接是惰性的），
# 只有真正碰库的测试才需要它 —— 那些测试自己 skip，不会连坐整个套件。
# CI 里由 postgres service 提供，见 .github/workflows/ci.yml。
