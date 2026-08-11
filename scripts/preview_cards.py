"""打印卡片 JSON,粘到飞书「消息卡片搭建工具」预览。无依赖。

  python -m scripts.preview_cards
"""
import json

from app import cards

spec = {
    "repo": "sample-app", "tag": "v1.2.3",
    "image": "registry.example.com/your-org/sample-app:v1.2.3",
    "platform": "k8s", "env": "prod",
    "operator_name": "Alice", "operator_id": "ou_placeholder_operator_id",
    "submit_time": "2026-01-01 12:00",
}

samples = {
    "1. 申请·待审批卡(→ 审批群)": cards.submit_card(spec),
    "2. 版本已部署卡(→ 结果群)": cards.result_card(spec, ok=True, approver_name="Bob", when="2026-01-01 12:02"),
    "3. 部署失败卡": cards.result_card(spec, ok=False, approver_name="Bob", when="2026-01-01 12:02"),
    "4. 被拒绝卡": cards.result_card(spec, rejected=True, status="REJECTED", approver_name="Bob",
                              reject_comment="先别发,等回归", when="2026-01-01 12:02"),
}

for title, card in samples.items():
    print("\n" + "=" * 60 + f"\n{title}\n" + "=" * 60)
    print(json.dumps(card, ensure_ascii=False, indent=2))
