"""Slack 审批提供方(APPROVAL_PROVIDER=slack,Socket Mode)。

全部离线:Slack Web API 用 FakeWeb 顶替(client.web),store 用带 CAS 语义的内存版顶替,
部署器用计数桩,Socket Mode 事件用 SDK 自己的 SocketModeRequest 构造。

守的判据(不是"代码跑得通"):
  ① 成员批准 → 恰好部署一次;重复点击 / 事件重投 / 并发点击也只部署一次
  ② 非成员 / 机器人 / 外部共享频道 / 别的频道 / 白名单外 → 拒绝,状态不变
  ③ 禁止自批,含"申请人映射不出来"时的 fail-close 策略
  ④ 拒绝走 CAS;拒绝后再点批准无效
  ⑤ Socket Mode 事件先 ack 再处理
  ⑥ 监督循环:崩了退避重建、稳定后退避归零、断线如实标成不健康
  ⑦ 默认 feishu 时行为不变;slack 模式缺 token / 配置错 → 启动失败(fail-close)
  ⑧ 飞书专属端点 501,/deploy-credential 照常
所有 ID / token 都是占位符。
"""
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.request import SocketModeRequest

from app import deploycred, events, k8s, server, settings, store, wshealth
from app.providers.slack import approval, client, messaging
from app.providers.slack import events as slack_events

_ROOT = Path(__file__).resolve().parent.parent

CH = "C0XXXXXXX"            # 审批频道
RES = "C0YYYYYYY"           # 结果频道
ALICE, BOB, CAROL, DAVE = "U0AAAAAAA", "U0BBBBBBB", "U0CCCCCCC", "U0DDDDDDD"
OUTSIDER, BOT = "U0OUTSIDE", "U0BOTBOT0"


# ─────────────────────────── 桩 ───────────────────────────

def _api_error(code: str) -> SlackApiError:
    return SlackApiError(f"The server responded with: {code}", {"ok": False, "error": code})


class FakeWeb:
    """Slack Web API 桩:只实现 client.py 用到的方法,记录所有写操作。"""

    def __init__(self, members=(ALICE, BOB, CAROL, DAVE, BOT)):
        self.member_pages: list = [list(members)]
        self.users = {u: {"id": u, "profile": {"display_name": u.lower()}}
                      for u in (ALICE, BOB, CAROL, DAVE, OUTSIDER)}
        self.users[BOT] = {"id": BOT, "is_bot": True, "profile": {"display_name": "bot"}}
        self.emails = {"dave@example.com": DAVE}
        self.channel = {"id": CH, "is_member": True, "is_ext_shared": False}
        self.post_error: Exception | None = None
        self.posts: list = []
        self.updates: list = []
        self.ephemerals: list = []
        self._ts = 0
        self._lock = threading.Lock()

    def auth_test(self):
        return {"ok": True, "user_id": BOT, "team_id": "T0XXXXXXX"}

    def conversations_info(self, channel):
        return {"ok": True, "channel": dict(self.channel, id=channel)}

    def conversations_members(self, channel, limit=None, cursor=None):
        i = int(cursor) if cursor else 0
        page = self.member_pages[i]
        if isinstance(page, Exception):
            raise page
        nxt = str(i + 1) if i + 1 < len(self.member_pages) else ""
        return {"ok": True, "members": page, "response_metadata": {"next_cursor": nxt}}

    def users_info(self, user):
        if user not in self.users:
            raise _api_error("user_not_found")
        return {"ok": True, "user": self.users[user]}

    def users_lookupByEmail(self, email):
        if email not in self.emails:
            raise _api_error("users_not_found")
        return {"ok": True, "user": {"id": self.emails[email]}}

    def chat_postMessage(self, channel, text, blocks=None, **kw):
        if self.post_error:
            raise self.post_error
        with self._lock:
            self._ts += 1
            ts = f"1700000000.{self._ts:06d}"
            self.posts.append({"channel": channel, "text": text, "blocks": blocks, "ts": ts,
                               "thread_ts": kw.get("thread_ts")})
        return {"ok": True, "channel": channel, "ts": ts}

    def chat_update(self, channel, ts, text, blocks, **kw):
        with self._lock:
            self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        return {"ok": True}

    def chat_postEphemeral(self, channel, user, text, **kw):
        with self._lock:
            self.ephemerals.append({"channel": channel, "user": user, "text": text})
        return {"ok": True}

    def ephemeral_for(self, user) -> str:
        return "\n".join(e["text"] for e in self.ephemerals if e["user"] == user)


class FakeStore:
    """内存版 store,带与 PG 版同语义的 CAS(单条件更新在锁内完成)。"""

    def __init__(self):
        self.rows: dict = {}
        self.lock = threading.Lock()

    def save(self, ic, spec, status="pending"):
        with self.lock:
            self.rows[ic] = {"instance_code": ic, "repo": spec["repo"], "tag": spec["tag"],
                             "spec_json": json.dumps(spec, ensure_ascii=False), "status": status}

    def get(self, ic):
        with self.lock:
            r = self.rows.get(ic)
            return dict(r) if r else None

    def set_status(self, ic, st):
        with self.lock:
            if ic in self.rows:
                self.rows[ic]["status"] = st

    def try_transition(self, ic, a, b):
        with self.lock:
            r = self.rows.get(ic)
            if r and r["status"] == a:
                r["status"] = b
                return True
            return False

    def try_claim_for_deploy(self, ic):
        return self.try_transition(ic, "pending", "deploying")

    def status_history(self, repo, tag, stage=None):
        with self.lock:
            return [r["status"] for r in reversed(list(self.rows.values()))
                    if r["repo"] == repo and r["tag"] == tag
                    and (stage is None or json.loads(r["spec_json"]).get("stage") == stage)]


class _Deployer:
    def __init__(self, calls: list, ok=True, log="ok", delay=0.0):
        self.calls, self.ok, self.log, self.delay = calls, ok, log, delay

    def deploy(self, spec):
        if self.delay:
            time.sleep(self.delay)
        self.calls.append(spec["tag"])
        return self.ok, self.log


class _SyncThread:
    """让后台线程在测试里同步执行(同 test_approval_bypass 的做法),断言可确定。"""
    def __init__(self, target, args=(), daemon=None):
        self._t, self._a = target, args

    def start(self):
        self._t(*self._a)


class FakeSM:
    """Socket Mode 客户端桩(on_request 只用到 send_socket_mode_response)。"""
    def __init__(self, log: list | None = None):
        self.acks: list = []
        self.log = log if log is not None else []

    def send_socket_mode_response(self, resp):
        self.acks.append(resp.envelope_id)
        self.log.append(("ack", resp.envelope_id))


# ─────────────────────────── fixtures ───────────────────────────

@pytest.fixture()
def slk(monkeypatch):
    monkeypatch.setattr(settings, "APPROVAL_PROVIDER", "slack")
    monkeypatch.setattr(settings, "SLACK_APPROVAL_CHANNEL", CH)
    monkeypatch.setattr(settings, "SLACK_RESULT_CHANNEL", RES)
    monkeypatch.setattr(settings, "SLACK_USER_MAP", {
        "alice": {"user_id": ALICE, "name": "Alice"},
        "carol": {"user_id": CAROL, "name": "Carol"}})
    monkeypatch.setattr(settings, "SLACK_FORBID_SELF_APPROVAL", True)
    monkeypatch.setattr(settings, "SLACK_APPROVERS", {})
    monkeypatch.setattr(settings, "RELEASE_TOKEN", "")
    monkeypatch.setattr(settings, "approval_bypass_on", lambda: False)
    monkeypatch.setattr(settings, "RELEASES", {"demo": {
        "project": "T", "method": "dryrun", "image_repo": "registry.example.com/demo",
        "stages": {"pre": {"env": "qa"}, "hotfix": {"env": "prod"}}}})

    web = FakeWeb()
    monkeypatch.setattr(client, "web", lambda: web)

    fs = FakeStore()
    for name in ("save", "get", "set_status", "try_transition", "try_claim_for_deploy",
                 "status_history"):
        monkeypatch.setattr(store, name, getattr(fs, name))
    monkeypatch.setattr(store, "usermap_get_row", lambda *a, **k: None)
    monkeypatch.setattr(store, "last_success_commit", lambda *a, **k: None)
    monkeypatch.setattr(store, "last_success_at", lambda *a, **k: None)

    deploys: list = []
    dep = _Deployer(deploys)
    monkeypatch.setattr(events, "get_deployer", lambda method: dep)

    server._rl_buckets.clear()
    wshealth.mark_starting()
    wshealth.mark_running()
    wshealth._st["since"] = time.time() - wshealth.GRACE_SEC - 1
    yield SimpleNamespace(web=web, store=fs, deploys=deploys, deployer=dep)
    wshealth._st.update({"state": "init", "since": 0.0, "attempts": 0,
                         "crashes": 0, "last_error": "", "last_error_at": 0.0})
    server._rl_buckets.clear()


@pytest.fixture()
def sync_threads(monkeypatch):
    monkeypatch.setattr(slack_events.threading, "Thread", _SyncThread)


def _submit(**kw) -> str:
    body = {"repo": "demo", "tag": "V1.0.0-pre", "stage": "pre", "github_actor": "alice"}
    body.update(kw)
    return server.release(server.ReleaseReq(**body), x_release_token="")["instance_code"]


_env_seq = iter(range(1, 10_000))


def _req(kind: str, user: str, ic: str, channel: str = CH, envelope: str | None = None):
    action_id = messaging.APPROVE_ACTION if kind == "approve" else messaging.REJECT_ACTION
    return SocketModeRequest(type="interactive", envelope_id=envelope or f"env-{next(_env_seq)}",
                             payload={
                                 "type": "block_actions",
                                 "user": {"id": user, "username": "x", "team_id": "T0XXXXXXX"},
                                 "container": {"type": "message", "message_ts": "1700000000.000001",
                                               "channel_id": channel},
                                 "channel": {"id": channel},
                                 "actions": [{"action_id": action_id, "value": ic, "type": "button"}],
                             })


def _click(kind, user, ic, **kw):
    sm = FakeSM()
    slack_events.on_request(sm, _req(kind, user, ic, **kw))
    return sm


def _status(slk_, ic):
    return slk_.store.get(ic)["status"]


def _has_actions(blocks) -> bool:
    return any(b.get("type") == "actions" for b in blocks)


# ─────────────────────── ① 受理 + 成员批准 → 部署一次 ───────────────────────

def test_release_posts_block_kit_with_confirmed_buttons(slk):
    ic = _submit()
    assert ic.startswith("slk-")
    assert _status(slk, ic) == "pending"
    assert len(slk.web.posts) == 1 and slk.web.posts[0]["channel"] == CH
    blocks = slk.web.posts[0]["blocks"]
    actions = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions) == 1
    approve, reject = actions[0]["elements"]
    assert approve["style"] == "primary" and approve["value"] == ic and approve.get("confirm")
    assert reject["style"] == "danger" and reject["value"] == ic and reject.get("confirm")
    dump = json.dumps(blocks, ensure_ascii=False)
    assert f"<@{ALICE}>" in dump, "申请人应 @ 成 Slack 用户"
    assert "registry.example.com/demo:V1.0.0-pre" in dump
    assert "禁止自批" in dump, "审批规则要写在消息上"


def test_member_approval_deploys_exactly_once(slk, sync_threads):
    ic = _submit()
    sm = _click("approve", BOB, ic)
    assert sm.acks, "Socket Mode 事件必须 ack"
    assert slk.deploys == ["V1.0.0-pre"]
    assert _status(slk, ic) == "success"
    # 原消息更新四次:已批准 → 部署中(补时间)→ 结果(补时间)→ 落库后的终态;都不再带按钮,且写明是谁批的
    assert len(slk.web.updates) == 4
    assert all(not _has_actions(u["blocks"]) for u in slk.web.updates)
    final = json.dumps(slk.web.updates[-1]["blocks"], ensure_ascii=False)
    assert f"<@{BOB}>" in final and "已部署" in final
    # 结果频道:部署中 + 结果
    res = [p for p in slk.web.posts if p["channel"] == RES]
    assert len(res) == 2
    assert f"<@{BOB}>" in json.dumps(res[-1]["blocks"], ensure_ascii=False)


def test_deploy_failure_is_visible_and_log_escaped(slk, sync_threads):
    slk.deployer.ok, slk.deployer.log = False, "rollout failed <!here>"
    ic = _submit()
    _click("approve", BOB, ic)
    assert _status(slk, ic) == "failed"
    assert "部署失败" in json.dumps(slk.web.updates[-1]["blocks"], ensure_ascii=False)
    res = json.dumps([p["blocks"] for p in slk.web.posts if p["channel"] == RES], ensure_ascii=False)
    assert "&lt;!here&gt;" in res and "<!here>" not in res, "部署日志进 mrkdwn 前必须转义"


def test_repeated_clicks_and_redelivered_event_deploy_once(slk, sync_threads):
    ic = _submit()
    req = _req("approve", BOB, ic, envelope="env-dup")
    slack_events.on_request(FakeSM(), req)
    slack_events.on_request(FakeSM(), req)          # 同一 envelope 重投
    _click("approve", BOB, ic)                      # 同一个人再点
    _click("approve", CAROL, ic)                    # 另一个人再点
    assert slk.deploys == ["V1.0.0-pre"], "重复事件必须只部署一次"
    assert "已处理" in slk.web.ephemeral_for(CAROL)


def test_concurrent_clicks_deploy_once(slk):
    """真线程并发点批准:CAS 保证只有一个线程走到部署。"""
    slk.deployer.delay = 0.05
    ic = _submit()
    payloads = [_req("approve", u, ic).payload for u in (BOB, CAROL) * 4]
    ts = [threading.Thread(target=approval.handle_action, args=("approve", p, p["actions"][0]))
          for p in payloads]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert slk.deploys == ["V1.0.0-pre"], f"并发点击部署了 {len(slk.deploys)} 次"


def test_concurrent_approve_and_reject_only_one_wins(slk):
    slk.deployer.delay = 0.02
    ic = _submit()
    a, r = _req("approve", BOB, ic).payload, _req("reject", CAROL, ic).payload
    ts = [threading.Thread(target=approval.handle_action, args=("approve", a, a["actions"][0])),
          threading.Thread(target=approval.handle_action, args=("reject", r, r["actions"][0]))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    st = _status(slk, ic)
    assert (st == "success" and slk.deploys == ["V1.0.0-pre"]) or \
           (st == "rejected" and slk.deploys == []), f"批准与拒绝同时生效了: {st} {slk.deploys}"


# ─────────────────────── ② 点击者鉴权 ───────────────────────

def _assert_denied(slk_, ic, user, needle):
    assert _status(slk_, ic) == "pending", "被拒的点击不得改变状态"
    assert slk_.deploys == []
    assert slk_.web.updates == [], "被拒的点击不得改动审批消息"
    assert needle in slk_.web.ephemeral_for(user), slk_.web.ephemeral_for(user)


def test_non_member_rejected(slk, sync_threads):
    ic = _submit()
    _click("approve", OUTSIDER, ic)
    _assert_denied(slk, ic, OUTSIDER, "不是审批频道的当前成员")
    _click("reject", OUTSIDER, ic)                  # 拒绝同样要求是成员
    _assert_denied(slk, ic, OUTSIDER, "不是审批频道的当前成员")


def test_bot_clicker_rejected(slk, sync_threads):
    ic = _submit()
    _click("approve", BOT, ic)
    _assert_denied(slk, ic, BOT, "机器人")


def test_members_are_paginated_and_failclose(slk, sync_threads):
    slk.web.member_pages = [[ALICE], [BOB]]         # BOB 在第二页
    ic = _submit()
    _click("approve", BOB, ic)
    assert slk.deploys == ["V1.0.0-pre"], "成员在第二页也必须被认出"

    slk.web.member_pages = [[ALICE], _api_error("ratelimited")]
    ic2 = _submit(tag="V1.0.1-pre")
    _click("approve", CAROL, ic2)
    assert _status(slk, ic2) == "pending"
    assert "读不到审批频道成员" in slk.web.ephemeral_for(CAROL), "拉不全名单必须按拒绝处理"


def test_ext_shared_channel_rejected(slk, sync_threads):
    ic = _submit()
    slk.web.channel["is_ext_shared"] = True
    _click("approve", BOB, ic)
    _assert_denied(slk, ic, BOB, "外部共享频道")


def test_click_from_other_channel_rejected(slk, sync_threads):
    ic = _submit()
    _click("approve", BOB, ic, channel="C0ZZZZZZZ")
    assert _status(slk, ic) == "pending" and slk.deploys == []


def test_stage_whitelist_must_also_hold(slk, sync_threads, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_APPROVERS", {"hotfix": [CAROL], "default": [BOB]})
    ic = _submit(tag="V2.0.0-hotfix", stage="hotfix")
    _click("approve", BOB, ic)                      # 是成员,但不在 hotfix 白名单
    _assert_denied(slk, ic, BOB, "白名单")
    _click("approve", CAROL, ic)
    assert slk.deploys == ["V2.0.0-hotfix"]
    # 没有自己条目的 stage 回落到 default
    ic2 = _submit(tag="V2.0.1-pre", stage="pre")
    _click("approve", CAROL, ic2)
    assert _status(slk, ic2) == "pending" and "白名单" in slk.web.ephemeral_for(CAROL)


# ─────────────────────── ③ 自批 ───────────────────────

def test_self_approval_rejected(slk, sync_threads):
    ic = _submit(github_actor="alice")
    _click("approve", ALICE, ic)
    _assert_denied(slk, ic, ALICE, "禁止自批")
    _click("approve", BOB, ic)
    assert slk.deploys == ["V1.0.0-pre"]


def test_self_approval_detected_via_email_lookup(slk, sync_threads):
    """github 账号没登记,但 operator_email 反查到了 Slack 用户 → 那个人也不能批。"""
    ic = _submit(github_actor="dave-gh", operator_email="dave@example.com")
    _click("approve", DAVE, ic)
    _assert_denied(slk, ic, DAVE, "禁止自批")


def test_unmapped_applicant_requires_known_approver(slk, sync_threads):
    """映射不出来时 fail-close:只有 user_map 里登记过、且不是该账号的成员能批。"""
    ic = _submit(github_actor="eve-gh")
    assert "未映射" in json.dumps(slk.web.posts[0]["blocks"], ensure_ascii=False)
    _click("approve", BOB, ic)                      # BOB 没登记:可能就是 eve 本人
    _assert_denied(slk, ic, BOB, "未映射")
    _click("approve", CAROL, ic)                    # CAROL 登记为 carol ≠ eve-gh
    assert slk.deploys == ["V1.0.0-pre"]


def test_unknown_applicant_nobody_can_approve_but_can_reject(slk, sync_threads):
    ic = _submit(github_actor=None)
    _click("approve", CAROL, ic)
    _assert_denied(slk, ic, CAROL, "申请人身份未知")
    _click("reject", CAROL, ic)
    assert _status(slk, ic) == "rejected"


def test_self_approval_check_can_be_disabled(slk, sync_threads, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_FORBID_SELF_APPROVAL", False)
    ic = _submit(github_actor="alice")
    _click("approve", ALICE, ic)
    assert slk.deploys == ["V1.0.0-pre"]


# ─────────────────────── ④ 拒绝 ───────────────────────

def test_reject_flow(slk, sync_threads):
    ic = _submit()
    _click("reject", BOB, ic)
    assert _status(slk, ic) == "rejected"
    assert len(slk.web.updates) == 1 and not _has_actions(slk.web.updates[0]["blocks"])
    assert "拒绝" in json.dumps(slk.web.updates[0]["blocks"], ensure_ascii=False)
    res = [p for p in slk.web.posts if p["channel"] == RES]
    assert len(res) == 1 and "被拒绝" in res[0]["text"]
    _click("approve", CAROL, ic)                    # 拒绝后再批:无效
    assert slk.deploys == [] and _status(slk, ic) == "rejected"
    assert "已处理" in slk.web.ephemeral_for(CAROL)


def test_applicant_may_withdraw_own_request(slk, sync_threads):
    """拒绝是收紧:申请人撤回自己的申请是允许的(自批限制只针对批准)。"""
    ic = _submit(github_actor="alice")
    _click("reject", ALICE, ic)
    assert _status(slk, ic) == "rejected"


# ─────────────────────── 注入 / 受理失败 / 链路 ───────────────────────

def test_external_text_is_escaped(slk):
    ic = _submit(notes="<!channel> 请看 <https://evil.example|官方说明>")
    dump = json.dumps(slk.web.posts[0]["blocks"], ensure_ascii=False)
    assert "<!channel>" not in dump and "&lt;!channel&gt;" in dump
    assert "<https://evil.example|" not in dump
    assert ic


def test_post_failure_marks_failed_and_returns_502(slk):
    slk.web.post_error = _api_error("not_in_channel")
    with pytest.raises(HTTPException) as e:
        _submit()
    assert e.value.status_code == 502
    assert [r["status"] for r in slk.store.rows.values()] == ["failed"], \
        "发不出去的审批不得留成 pending(静默积压)"


def test_release_503_when_socket_mode_down(slk):
    wshealth.mark_crashed("SlackApiError: invalid_auth")
    with pytest.raises(HTTPException) as e:
        _submit()
    assert e.value.status_code == 503
    assert slk.web.posts == [] and slk.store.rows == {}


def test_healthz_reports_provider_in_slack_mode(slk):
    out = server.healthz()
    assert out["ok"] is True and out["provider"] == "slack"


# ─────────────────────── ⑤ Socket Mode:先 ack 再处理 ───────────────────────

def test_ack_happens_before_processing(slk, sync_threads, monkeypatch):
    log: list = []
    monkeypatch.setattr(approval, "handle_action",
                        lambda kind, payload, act: log.append(("handle", kind)))
    sm = FakeSM(log)
    slack_events.on_request(sm, _req("approve", BOB, "slk-x", envelope="e1"))
    assert log == [("ack", "e1"), ("handle", "approve")]


def test_every_envelope_is_acked_but_only_our_buttons_handled(slk, sync_threads, monkeypatch):
    handled: list = []
    monkeypatch.setattr(approval, "handle_action", lambda *a: handled.append(a))
    sm = FakeSM()
    slack_events.on_request(sm, SocketModeRequest(type="events_api", envelope_id="e2",
                                                  payload={"event": {"type": "message"}}))
    other = _req("approve", BOB, "slk-x", envelope="e3")
    other.payload["actions"][0]["action_id"] = "someone_else_button"
    slack_events.on_request(sm, other)
    assert sm.acks == ["e2", "e3"] and handled == []


def test_denial_reaches_non_member_via_response_url(slk, sync_threads, monkeypatch):
    """非成员恰恰是最常被拒的人,而 chat.postEphemeral 发不给不在频道里的人 ——
    必须走 response_url,否则他点了没任何反馈。"""
    sent: list = []
    monkeypatch.setattr(client, "respond_ephemeral", lambda url, text: sent.append((url, text)))
    ic = _submit()
    req = _req("approve", OUTSIDER, ic)
    req.payload["response_url"] = "https://hooks.slack.com/actions/T0XXXXXXX/1/placeholder"
    slack_events.on_request(FakeSM(), req)
    assert sent and "不是审批频道的当前成员" in sent[0][1]
    assert slk.web.ephemerals == [], "response_url 可用时不该再走 postEphemeral"


def test_response_url_must_be_slack(slk, sync_threads):
    """response_url 来自负载:不是 Slack 的回调域名就不发,退回 postEphemeral。"""
    with pytest.raises(ValueError):
        client.respond_ephemeral("https://evil.example/collect", "x")
    ic = _submit()
    req = _req("approve", OUTSIDER, ic)
    req.payload["response_url"] = "https://evil.example/collect"
    slack_events.on_request(FakeSM(), req)
    assert "不是审批频道的当前成员" in slk.web.ephemeral_for(OUTSIDER)


def test_foreign_instance_code_ignored(slk, sync_threads):
    _click("approve", BOB, "ins-from-somewhere-else")
    assert slk.deploys == [] and "无法识别" in slk.web.ephemeral_for(BOB)


# ─────────────────────── ⑥ 监督循环 ───────────────────────

class _Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def now(self):
        return self.t


class _SMClient:
    """可编排的 Socket Mode 客户端:connect 是否失败、is_connected 的序列。"""
    def __init__(self, connect_error=None, connected_seq=None, then=False):
        self.connect_error, self.seq, self.then = connect_error, list(connected_seq or []), then
        self.closed = False

    def connect(self):
        if self.connect_error:
            raise self.connect_error

    def is_connected(self):
        if self.seq:
            v = self.seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v
        return self.then

    def close(self):
        self.closed = True


@pytest.fixture()
def sup(monkeypatch):
    clk = _Clock()
    monkeypatch.setattr(slack_events, "_sleep", clk.sleep)
    monkeypatch.setattr(slack_events, "_clock", clk.now)
    monkeypatch.setattr(slack_events, "_POLL_SEC", 1.0)    # 与退避值区分开
    monkeypatch.setattr(slack_events, "preflight", lambda: None)
    wshealth._st.update({"state": "init", "since": 0.0, "attempts": 0,
                         "crashes": 0, "last_error": "", "last_error_at": 0.0})
    yield clk
    wshealth._st.update({"state": "init", "since": 0.0, "attempts": 0,
                         "crashes": 0, "last_error": "", "last_error_at": 0.0})


def test_supervisor_survives_connect_failures_with_backoff(sup, monkeypatch, capsys):
    built: list = []

    def build():
        c = _SMClient(connect_error=_api_error("invalid_auth"))
        built.append(c)
        return c

    monkeypatch.setattr(slack_events, "_build_client", build)
    slack_events.run_supervisor(max_cycles=4)
    assert sup.sleeps == [2, 4, 8], "崩溃后必须指数退避重建,而不是让线程死掉或狂重连"
    assert all(c.closed for c in built), "每个失败的客户端都要 close,否则后台线程随重试泄漏"
    st = wshealth.snapshot()
    assert st["state"] == "crashed" and st["crashes"] == 4 and not st["healthy"]
    assert "invalid_auth" in st["last_error"] and "\n" not in st["last_error"]
    down = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[ws][down]")]
    assert len(down) == 4 and all("provider=slack" in ln and "后重建" in ln for ln in down), \
        "告警关键字必须可 grep,且整条信息在同一行"


def test_supervisor_preflight_failure_is_unhealthy(sup, monkeypatch):
    """bot token 失效 / 机器人不在频道:Socket Mode 照样连得上 —— 必须在建连前就判不健康。"""
    def bad():
        raise RuntimeError("机器人不在审批频道里")
    monkeypatch.setattr(slack_events, "preflight", bad)
    monkeypatch.setattr(slack_events, "_build_client",
                        lambda: pytest.fail("预检不过不该去建连"))
    slack_events.run_supervisor(max_cycles=1)
    st = wshealth.snapshot()
    assert st["state"] == "crashed" and "不在审批频道" in st["last_error"]


def test_backoff_resets_after_stable_run(sup, monkeypatch, capsys):
    """稳定运行过再断:退避从头开始,而不是沿用上次攒下的值(语义同 main._STABLE_SEC)。"""
    clients = iter([
        _SMClient(connect_error=RuntimeError("boom")),               # 第 1 轮:失败 → 退避 2,下次 4
        _SMClient(connected_seq=[True] * 70, then=False),           # 第 2 轮:稳定 70s 后断线不回
    ])
    monkeypatch.setattr(slack_events, "_build_client", lambda: next(clients))
    slack_events.run_supervisor(max_cycles=2)
    assert [s for s in sup.sleeps if s != 1.0] == [2]
    down = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[ws][down]")]
    assert len(down) == 2
    assert "→ 2s 后重建" in down[-1], f"稳定运行后退避没有归零: {down[-1]}"
    assert "Socket Mode 断开" in wshealth.snapshot()["last_error"]


def test_watch_reports_reconnecting_then_recovers(sup, monkeypatch):
    seen: list = []
    monkeypatch.setattr(wshealth, "mark_reconnecting", lambda err: seen.append("reconnecting"))
    monkeypatch.setattr(wshealth, "mark_running", lambda: seen.append("running"))

    class _Stop(Exception):
        pass

    with pytest.raises(_Stop):
        slack_events._watch(_SMClient(connected_seq=[True, False, False, True, _Stop()]))
    assert seen == ["reconnecting", "running"], "断线期间必须如实标成不健康,恢复后回到 running"


def test_reconnecting_state_is_unhealthy():
    wshealth.mark_running()
    wshealth._st["since"] = time.time() - wshealth.GRACE_SEC - 1
    wshealth.mark_reconnecting("lost")
    try:
        assert wshealth.healthy() is False
        assert wshealth.snapshot()["state"] == "reconnecting"
    finally:
        wshealth._st.update({"state": "init", "since": 0.0, "attempts": 0,
                             "crashes": 0, "last_error": "", "last_error_at": 0.0})


def test_preflight_requires_bot_in_channels(monkeypatch):
    web = FakeWeb()
    monkeypatch.setattr(client, "web", lambda: web)
    monkeypatch.setattr(settings, "SLACK_APPROVAL_CHANNEL", CH)
    monkeypatch.setattr(settings, "SLACK_RESULT_CHANNEL", CH)
    slack_events.preflight()
    web.channel["is_member"] = False
    with pytest.raises(RuntimeError, match="不在审批频道"):
        slack_events.preflight()


# ─────────────────────── ⑦ 默认 feishu 不变 + 启动 fail-close ───────────────────────

def test_default_provider_release_path_unchanged(monkeypatch):
    """APPROVAL_PROVIDER 未设 = feishu:/release 仍走 群成员 → 建实例 → 发卡,完全不碰 Slack。"""
    assert settings.APPROVAL_PROVIDER == "feishu"
    monkeypatch.setattr(client, "web", lambda: pytest.fail("飞书模式不得调用 Slack"))
    monkeypatch.setattr(settings, "RELEASE_TOKEN", "")
    monkeypatch.setattr(settings, "approval_bypass_on", lambda: False)
    monkeypatch.setattr(settings, "RELEASES", {"demo": {"method": "dryrun", "image_repo": "r/demo"}})
    monkeypatch.setattr(settings, "DEFAULT_INITIATOR", "u1")
    monkeypatch.setattr(server, "_rate_ok", lambda *a, **k: True)
    monkeypatch.setattr(store, "status_history", lambda *a, **k: [])
    monkeypatch.setattr(store, "usermap_get_row", lambda *a, **k: None)
    monkeypatch.setattr(store, "last_success_commit", lambda *a, **k: None)
    saved, cards_sent = [], []
    monkeypatch.setattr(store, "save", lambda ic, spec, status="pending": saved.append((ic, spec)))
    monkeypatch.setattr(server.feishu, "get_chat_members", lambda *a, **k: ["u1", "u2"])
    monkeypatch.setattr(server.feishu, "create_instance", lambda *a, **k: "ins-feishu-1")
    monkeypatch.setattr(server.feishu, "send_card", lambda chat, card: cards_sent.append(chat))
    wshealth.mark_running()
    wshealth._st["since"] = time.time() - wshealth.GRACE_SEC - 1
    try:
        out = server.release(server.ReleaseReq(repo="demo", tag="v1"), x_release_token="")
    finally:
        wshealth._st.update({"state": "init", "since": 0.0})
    assert out["instance_code"] == "ins-feishu-1"
    assert saved and "slack_applicant_ids" not in saved[0][1], "飞书模式的 spec 不得多出 Slack 字段"
    assert cards_sent == [settings.DETAIL_CHAT_ID]
    assert "provider" not in server.healthz(), "飞书模式的 /healthz 响应体保持不变"


def test_default_provider_deploy_notifications_go_to_feishu(monkeypatch):
    monkeypatch.setattr(client, "web", lambda: pytest.fail("飞书模式不得调用 Slack"))
    sent = []
    monkeypatch.setattr(events.feishu, "send_card", lambda chat, card: sent.append(card))
    monkeypatch.setattr(events.store, "set_status", lambda *a: None)
    events._run_deploy("ins-1", {"repo": "demo", "tag": "v1", "image": "r/demo:v1",
                                 "method": "dryrun", "cluster": "c", "namespace": "n"}, "张三")
    assert len(sent) == 2, "部署中 + 结果 两张飞书卡"


def _run_py(code: str, env_extra: dict, config_text: str | None, tmp_path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("FEISHU_", "SLACK_", "APPROVAL_PROVIDER"))}
    env["PYTHONPATH"] = str(_ROOT)
    if config_text is not None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent(config_text), encoding="utf-8")
        env["CONFIG_PATH"] = str(cfg)
    env.update(env_extra)
    return subprocess.run([sys.executable, "-c", code], env=env, cwd=str(_ROOT),
                          capture_output=True, text=True, timeout=120)


_SLACK_CFG = """
slack:
  approval_channel: "C0XXXXXXX"
  user_map:
    alice: "U0AAAAAAA"
releases:
  demo: {method: dryrun, image_repo: "registry.example.com/demo"}
"""
_TOKENS = {"APPROVAL_PROVIDER": "slack", "SLACK_BOT_TOKEN": "xoxb-placeholder",
           "SLACK_APP_TOKEN": "xapp-placeholder"}


def test_default_provider_is_feishu_and_still_requires_feishu_creds(tmp_path):
    ok = _run_py("from app import settings; print(settings.APPROVAL_PROVIDER)",
                 {"FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "x"}, None, tmp_path)
    assert ok.returncode == 0, ok.stderr[-500:]
    assert ok.stdout.strip() == "feishu"
    bad = _run_py("import app.settings", {}, None, tmp_path)
    assert bad.returncode != 0 and "FEISHU_APP_ID" in bad.stderr, "飞书模式的启动要求不得被放宽"


def test_slack_mode_needs_no_feishu_config(tmp_path):
    r = _run_py("import app.settings, app.server, app.main; from app import settings; "
                "print('RESULT', settings.APPROVAL_PROVIDER, settings.SLACK_RESULT_CHANNEL)",
                _TOKENS, _SLACK_CFG, tmp_path)
    assert r.returncode == 0, r.stderr[-800:]
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT")][-1]
    assert line.split() == ["RESULT", "slack", "C0XXXXXXX"], "结果频道缺省应等于审批频道"


@pytest.mark.parametrize("env_patch, cfg_patch, needle", [
    ({"SLACK_BOT_TOKEN": ""}, None, "SLACK_BOT_TOKEN"),
    ({"SLACK_APP_TOKEN": ""}, None, "SLACK_APP_TOKEN"),
    ({"SLACK_BOT_TOKEN": "xoxp-placeholder"}, None, "xoxb-"),
    ({"SLACK_APP_TOKEN": "xoxb-placeholder"}, None, "xapp-"),
    ({}, ('approval_channel: "C0XXXXXXX"', 'approval_channel: "#deploys"'), "频道 ID"),
    ({}, ("  user_map:", "  forbid_self_approval: \"false\"\n  user_map:"), "布尔"),
    ({}, ('alice: "U0AAAAAAA"', 'alice: "alice"'), "user ID"),
    ({}, ("  user_map:", "  approvers: {release: []}\n  user_map:"), "非空"),
    ({"APPROVAL_PROVIDER": "slak"}, None, "APPROVAL_PROVIDER"),
])
def test_slack_mode_fails_closed_on_bad_startup_config(tmp_path, env_patch, cfg_patch, needle):
    cfg = _SLACK_CFG if cfg_patch is None else _SLACK_CFG.replace(*cfg_patch)
    r = _run_py("import app.settings", {**_TOKENS, **env_patch}, cfg, tmp_path)
    assert r.returncode != 0, f"配置有误却启动成功了: {env_patch} {cfg_patch}"
    assert needle in r.stderr, r.stderr[-600:]
    assert "placeholder" not in r.stderr, "错误信息不得回显 token"


def test_slack_mode_requires_slack_section(tmp_path):
    r = _run_py("import app.settings", _TOKENS,
                "releases:\n  demo: {method: dryrun}\n", tmp_path)
    assert r.returncode != 0 and "slack:" in r.stderr


# ─────────────────────── ⑧ 飞书专属端点 501,IM 无关通道照常 ───────────────────────

@pytest.mark.parametrize("path, body", [
    ("/viewer-token", {"user_id": "u", "cluster": "c", "namespace": "n"}),
    ("/secret/request", {"user_id": "u", "name": "s", "key": "k", "cluster": "c", "namespace": "n"}),
    ("/secret/commit", {"request_id": "x", "value": "v"}),
    ("/release-key", {"grant": "g"}),
])
def test_feishu_only_endpoints_return_501(slk, path, body):
    r = TestClient(server.app).post(path, json=body)
    assert r.status_code == 501, f"{path}: {r.status_code} {r.text[:200]}"
    assert "仅飞书模式支持" in r.text


@pytest.mark.parametrize("method, path", [
    ("get", "/admin"), ("get", "/api/usermap"), ("get", "/api/resolve?email=a@example.com"),
    ("post", "/viewer-panel"),
])
def test_feishu_only_admin_endpoints_return_501(slk, monkeypatch, method, path):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "admin-pw-for-test")
    r = getattr(TestClient(server.app), method)(path, auth=("admin", "admin-pw-for-test"))
    assert r.status_code == 501, f"{path}: {r.status_code}"


def test_deploy_credential_still_works_in_slack_mode(slk, monkeypatch):
    monkeypatch.setattr(settings, "RELEASE_TOKEN", "t")
    target = {"cluster": "qa", "namespace": "qa-namespace", "sa": "approvo-deployer"}
    monkeypatch.setattr(k8s, "deploy_target_allowed", lambda c, n: target)
    monkeypatch.setattr(k8s, "issue_deploy_kubeconfig",
                        lambda c, n, slot, ttl: (True, "apiVersion: v1\nkind: Config\n"))
    grant, _ = deploycred.issue_grant("slk-x", "qa", "qa-namespace")
    r = TestClient(server.app).post("/deploy-credential", json={"grant": grant},
                                    headers={"X-Release-Token": "t"})
    try:
        assert r.status_code == 200, r.text[:200]
        assert "kind: Config" in r.json()["kubeconfig"]
    finally:
        if r.status_code == 200:
            deploycred.release_slot(r.json()["slot"])


def test_provider_factory_dispatches(slk):
    from app.providers import get_provider
    from app.providers.slack import SlackProvider
    p = get_provider()
    assert isinstance(p, SlackProvider)
    ic = p.create_approval("", "", "")
    assert ic.startswith("slk-")
    assert p.list_chat_members(CH) == sorted([ALICE, BOB, CAROL, DAVE, BOT])


# ─────────────── 与真实 SDK 对齐:防 FakeWeb 与真 API 漂移、防 Block Kit 超限 ───────────────

def test_fake_web_matches_real_webclient_signatures():
    """FakeWeb 只是桩 —— 若真 WebClient 的方法不再接受我们传的参数,上面的用例会全绿而线上全挂。"""
    import inspect

    from slack_sdk import WebClient
    real = WebClient(token="xoxb-placeholder")
    used = {"conversations_members": {"channel", "limit", "cursor"},
            "conversations_info": {"channel"}, "users_info": {"user"},
            "users_lookupByEmail": {"email"},
            "chat_postMessage": {"channel", "text", "blocks", "unfurl_links", "unfurl_media"},
            "chat_update": {"channel", "ts", "text", "blocks"},
            "chat_postEphemeral": {"channel", "user", "text"}, "auth_test": set()}
    for name, kws in used.items():
        params = set(inspect.signature(getattr(real, name)).parameters)
        assert kws <= params, f"WebClient.{name} 不再接受 {kws - params}"


def test_real_socket_mode_client_builds_offline(monkeypatch):
    from slack_sdk import WebClient
    monkeypatch.setattr(settings, "SLACK_APP_TOKEN", "xapp-placeholder")
    monkeypatch.setattr(client, "web", lambda: WebClient(token="xoxb-placeholder"))
    cli = slack_events._build_client()
    try:
        assert slack_events.on_request in cli.socket_mode_request_listeners
        assert cli.auto_reconnect_enabled is True
        assert cli.is_connected() is False
    finally:
        cli.close()


def _validate(blocks: list) -> None:
    """用 slack_sdk 自带的 Block Kit 模型校验长度上限(to_dict 会逐层 validate)。
    超限时 Slack 会以 invalid_blocks 拒掉整条消息 —— 对审批消息而言就是"发不出去"。"""
    from slack_sdk.models.blocks import Block
    assert len(blocks) <= 50
    for b in blocks:
        Block.parse(b).to_dict()


def test_block_kit_stays_within_limits_for_huge_inputs(slk):
    long = "超长" * 400
    spec = {
        "repo": "demo-" + "x" * 300, "tag": "V" + "9" * 300, "image": "registry.example.com/" + long,
        "stage": "hotfix", "project": "P" * 300, "env": "prod", "platform": long,
        "method": "github", "deploy": {"cluster": long, "namespace": long},
        "operator_github": "g" * 200, "operator_name": long, "operator_mapped": True,
        "operator_id": ALICE, "slack_applicant_ids": [ALICE, DAVE], "submit_time": "2026-01-01 00:00",
        "notes": "\n".join(f"+ KEY_{i}=" + "v" * 300 for i in range(500)),
        "db_changes": {"status": "ok", "files": [
            {"name": "f" * 200 + f"{i}.sql", "status": "added", "danger": ["DROP", "TRUNCATE"]}
            for i in range(40)]},
        "changes": {"status": "ok", "total_commits": 999, "compare_url": "https://example.com/c",
                    "prs": [{"number": i, "title": long, "author": "a" * 100,
                             "url": f"https://example.com/pull/{i}"} for i in range(40)],
                    "direct_commits": [{"sha": "abcdef1", "message": long, "author": "b"}
                                       for _ in range(20)]},
    }
    huge_manifest = dict(spec, manifest={
        "status": "ok", "path": "releases/" + "p" * 300 + "/release.yaml", "commit": "c" * 40,
        "image_repo": "registry.example.com/" + long, "notes": long,
        "services": [{"name": f"svc-{i}-" + "s" * 80, "image": "img-" + "i" * 150,
                      "deployments": [f"dep-{i}-{j}-" + "d" * 80 for j in range(5)]} for i in range(50)],
        "migrations": [{"database": "db_" + "x" * 90, "file": "migrations/" + "m" * 150 + f"{i}.sql",
                        "danger": ["DROP", "TRUNCATE"]} for i in range(50)],
        "configmaps": ["deploy/" + "c" * 200 + f"{i}.yaml" for i in range(50)]})
    ic = approval.new_instance_code()
    for sp in (spec, huge_manifest):
        for dec in (None,
                    {"kind": "approved", "user": BOB, "name": "b", "when": "t", "deploying_at": "t",
                     "finished_at": "t", "final": "failed", "log": "E" * 50_000}):
            _validate(messaging.approval_message(ic, sp, dec)[1])
    _validate(messaging.deploying_message(huge_manifest, long, "t")[1])
    _validate(messaging.result_message(huge_manifest, ok=False, approver_name="b", when="t", log="E" * 9)[1])
    for dec in (None,
                {"kind": "approved", "user": BOB, "name": long, "when": "t"},
                {"kind": "approved", "user": BOB, "name": "b", "when": "t", "final": "success"},
                {"kind": "approved", "user": BOB, "name": "b", "when": "t", "final": "failed"},
                {"kind": "rejected", "user": BOB, "name": "b", "when": "t"}):
        _validate(messaging.approval_message(ic, spec, dec)[1])
    _validate(messaging.deploying_message(spec, long, "t")[1])
    _validate(messaging.result_message(spec, ok=True, approver_name=long, when="t")[1])
    _validate(messaging.result_message(spec, ok=False, approver_name="b", when="t",
                                       log="E" * 50_000)[1])
    _validate(messaging.result_message(spec, rejected=True, approver_name="b", when="t")[1])


# ─────────────────────── 单卡模式(结果频道 = 审批频道) ───────────────────────

@pytest.fixture()
def single(slk, monkeypatch):
    monkeypatch.setattr(settings, "SLACK_RESULT_CHANNEL", CH)
    return slk


def test_single_card_mode_only_updates_the_approval_message(single, sync_threads):
    """没另配结果频道时:批准 → 部署中 → 已部署 全在同一条消息上,频道里不再多出两条。"""
    ic = _submit()
    _click("approve", BOB, ic)
    assert _status(single, ic) == "success"
    assert len(single.web.posts) == 1, "只应有最初那条审批消息"
    assert {u["ts"] for u in single.web.updates} == {"1700000000.000001"}
    final = json.dumps(single.web.updates[-1]["blocks"], ensure_ascii=False)
    for word in ("发起", "批准", "部署中", "已部署"):
        assert word in final, f"时间线缺「{word}」"
    assert "已部署" in single.web.updates[-1]["text"]
    assert not _has_actions(single.web.updates[-1]["blocks"])


def test_single_card_failure_shows_log_on_card_and_pings_in_thread(single, sync_threads):
    single.deployer.ok, single.deployer.log = False, "rollout failed <!here>"
    ic = _submit()
    _click("approve", BOB, ic)
    assert _status(single, ic) == "failed"
    final = json.dumps(single.web.updates[-1]["blocks"], ensure_ascii=False)
    assert "部署失败" in final and "失败详情" in final
    assert "&lt;!here&gt;" in final and "<!here>" not in final, "卡上的部署日志必须转义"
    thread = [p for p in single.web.posts if p.get("thread_ts")]
    assert len(thread) == 1 and thread[0]["thread_ts"] == "1700000000.000001"
    assert f"<@{BOB}>" in thread[0]["text"], "失败要在线程里叫到审批人(改消息不推送通知)"
    assert len([p for p in single.web.posts if not p.get("thread_ts")]) == 1


def test_single_card_reject_posts_nothing_else(single, sync_threads):
    ic = _submit()
    _click("reject", BOB, ic)
    assert _status(single, ic) == "rejected"
    assert len(single.web.posts) == 1
    assert "拒绝" in json.dumps(single.web.updates[-1]["blocks"], ensure_ascii=False)


def test_timeline_hides_date_on_same_day_and_keeps_it_across_days():
    spec = {"submit_time": "2026-01-01 09:00"}
    same = messaging._timeline(spec, {"kind": "approved", "user": BOB, "when": "2026-01-01 09:05",
                                      "deploying_at": "2026-01-01 09:05",
                                      "finished_at": "2026-01-01 09:07", "final": "success"})
    assert "`09:00` 发起" in same and "`09:07` ✅ 已部署" in same
    nxt = messaging._timeline(spec, {"kind": "approved", "user": BOB, "when": "2026-01-02 08:00"})
    assert "2026-01-02 08:00" in nxt and nxt.endswith("…")
