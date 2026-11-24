"""回滚请求(rollback):切回历史版本镜像,走同一套审批,审批通过后派发给部署流水线。

守的判据:
  ① 结构校验 fail-close:未知字段、id 与 tag 不一致、服务名 / tag / 路径不合法 → 拒绝
  ② 派发给部署 workflow 的是规范化后的 JSON(rollback_spec 输入),不带 tag 比较所需的清单
  ③ 审批卡与 Slack 卡都把回滚的服务、目标版本、ConfigMap、跨迁移开关写出来
  ④ 不带 rollback 的请求行为不变(本文件不覆盖,由既有用例守)
所有仓库名 / 服务名都是占位符。
"""
import json

import pytest

from app import cards, rollback
from app.providers.slack import messaging

RB_ID = "RB-svc-1"


def _raw(**over):
    raw = {"id": RB_ID, "services": {"my-app": "V1.2.1-release"}}
    raw.update(over)
    return raw


def test_valid_request_is_normalized():
    rb = rollback.validate(_raw(services={"b-svc": "V1.2.1-release", "a-svc": "V1.2.0-release"},
                                configmaps=[{"path": "k8s/cfg/a.yaml", "from": "V1.2.0-release"}],
                                notes="回退 X"), RB_ID)
    assert list(rb["services"]) == ["a-svc", "b-svc"]            # 按名排序，派发文本稳定
    assert rb["configmaps"] == [{"path": "k8s/cfg/a.yaml", "from": "V1.2.0-release"}]
    assert rb["allow_schema_behind"] is False


@pytest.mark.parametrize("raw,tag", [
    (_raw(), "V9.9.9-release"),                                   # tag 与 id 不一致
    (_raw(extra=1), RB_ID),                                       # 未知字段
    (_raw(id="bad id"), "bad id"),                                # id 格式
    (_raw(services={}), RB_ID),                                   # 空服务
    (_raw(services={"Bad_Name!": "V1.2.1-release"}), RB_ID),      # 服务名
    (_raw(services={"my-app": "x/../y"}), RB_ID),                 # tag 含非法字符
    (_raw(configmaps=[{"path": "../etc/x", "from": "V1.2.0-release"}]), RB_ID),
    (_raw(configmaps=[{"path": "/abs.yaml", "from": "V1.2.0-release"}]), RB_ID),
    (_raw(configmaps=[{"path": "a.yaml"}]), RB_ID),               # 缺 from
    (_raw(allow_schema_behind="yes"), RB_ID),                     # 非布尔
    (_raw(notes="x" * 301), RB_ID),
    ("not-a-dict", RB_ID),
])
def test_invalid_requests_are_refused(raw, tag):
    with pytest.raises(rollback.RollbackError):
        rollback.validate(raw, tag)


def test_dispatch_input_is_json_round_trip():
    rb = rollback.validate(_raw(allow_schema_behind=True), RB_ID)
    text = rollback.dispatch_input(rb)
    assert json.loads(text) == rb
    assert "\n" not in text                                       # 单行，便于 workflow_dispatch 传入


def test_summary_lines_say_what_changes():
    rb = rollback.validate(_raw(configmaps=[{"path": "k8s/cfg/a.yaml", "from": "V1.2.0-release"}]), RB_ID)
    text = "\n".join(rollback.summary_lines(rb))
    assert "my-app → V1.2.1-release" in text
    assert "k8s/cfg/a.yaml（取自 V1.2.0-release）" in text
    assert "迁移：不执行" in text
    assert "默认拒绝" in text


def _spec(rb):
    return {"repo": "my-app", "tag": RB_ID, "env": "prod", "platform": "-", "submit_time": "2026-01-01 00:00",
            "operator_name": "tester", "operator_github": "tester", "operator_mapped": False,
            "operator_id": "u1", "rollback": rb, "changes": None, "db_changes": None, "manifest": None,
            "notes": "", "image": RB_ID, "stage": None, "method": "github"}


def test_submit_card_shows_rollback_block_and_no_image():
    rb = rollback.validate(_raw(allow_schema_behind=True), RB_ID)
    card = cards.submit_card(_spec(rb))
    text = json.dumps(card, ensure_ascii=False)
    assert "回滚（切回历史版本镜像" in text
    assert "my-app → V1.2.1-release" in text
    assert "默认拒绝" not in text                                 # 已显式允许
    assert "已允许" in text


def test_slack_submit_blocks_show_rollback():
    rb = rollback.validate(_raw(), RB_ID)
    spec = _spec(rb)
    blocks = messaging._what(spec)
    flat = json.dumps(blocks, ensure_ascii=False)
    assert "回滚（切回历史版本镜像" in flat
    assert "默认拒绝" in flat
