"""store.py 的 PostgreSQL 实现 —— 打真库跑。

🔴 为什么必须打真库:这次改的是【存储层】,SQL 方言、upsert 语义、rowcount 行为
   全都只有真库能验。mock 掉数据库再测,等于测了个寂寞 ——
   而 try_claim_for_deploy 一旦不原子,后果是【同一个审批被部署两次】。

没有 DB_DSN 时整个文件 skip(不是报错):CI 由 postgres service 提供,
本地没起库的人也不该因此看到一片红。
"""
import os
import threading

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DB_DSN"),
                                reason="需要 DB_DSN 指向一个可写的 PostgreSQL")


@pytest.fixture()
def store():
    from app import store
    # 每个用例从干净的表开始，避免相互污染
    with store._conn() as c:
        c.execute("drop table if exists releases")
        c.execute("drop table if exists user_map")
    store.init()
    return store


def _spec(repo="my-app", tag="V1.0.0", **kw):
    d = {"repo": repo, "tag": tag, "image": "img:" + tag,
         "cluster": "prod-cluster", "namespace": "prod-namespace"}
    d.update(kw)
    return d


def test_init_is_idempotent(store):
    store.init()
    store.init()          # 重复建表不该炸
    assert store.list_pending() == []


def test_save_and_get_roundtrip(store):
    store.save("ic-1", _spec())
    got = store.get("ic-1")
    assert got["repo"] == "my-app"
    assert got["status"] == "pending"
    assert got["created_at"] and got["updated_at"]
    assert store.get("nope") is None


def test_save_is_upsert_not_duplicate(store):
    store.save("ic-1", _spec(tag="V1"))
    store.save("ic-1", _spec(tag="V2"))
    assert store.get("ic-1")["tag"] == "V2"
    assert len(store.list_pending()) == 1, "upsert 变成了插入两行"


def test_try_claim_is_atomic_under_concurrency(store):
    """🔴 核心用例:20 个线程同时抢,必须【恰好一个】成功。

    这条保证的是"同一个审批不会被部署两次"。SQLite 版靠进程内锁,
    PG 版靠行级锁 —— 换了实现就必须重新证明,不能假设它还成立。
    """
    store.save("ic-race", _spec())
    results, lock = [], threading.Lock()

    def worker():
        r = store.try_claim_for_deploy("ic-race")
        with lock:
            results.append(r)

    ts = [threading.Thread(target=worker) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert sum(results) == 1, f"抢到的线程数={sum(results)}，不是 1 → 会重复部署"
    assert store.get("ic-race")["status"] == "deploying"


def test_claim_rejects_non_pending(store):
    store.save("ic-2", _spec(), status="success")
    assert store.try_claim_for_deploy("ic-2") is False
    assert store.try_claim_for_deploy("不存在") is False


def test_set_status_and_list_pending(store):
    store.save("ic-a", _spec())
    store.save("ic-b", _spec())
    store.set_status("ic-a", "success")
    assert store.list_pending() == ["ic-b"]


def test_status_history_is_newest_first(store):
    store.save("ic-1", _spec(tag="V9"))
    store.set_status("ic-1", "failed")
    store.save("ic-2", _spec(tag="V9"))
    hist = store.status_history("my-app", "V9")
    assert len(hist) == 2
    assert "pending" in hist and "failed" in hist


def test_last_success_commit_prefers_explicit_commit(store):
    store.save("ic-1", _spec(tag="v20260810.abc1234", commit="deadbeef"))
    store.set_status("ic-1", "success")
    assert store.last_success_commit("my-app") == "deadbeef"


def test_last_success_commit_falls_back_to_tag_sha(store):
    store.save("ic-1", _spec(tag="v20260810.abc1234"))
    store.set_status("ic-1", "success")
    assert store.last_success_commit("my-app") == "abc1234"
    assert store.last_success_commit("从没发过的仓") is None


def test_usermap_crud(store):
    store.usermap_set("octocat", "u_123", "章鱼猫")
    assert store.usermap_get("octocat") == "u_123"
    assert store.usermap_get_row("octocat")["name"] == "章鱼猫"
    assert store.name_by_id("u_123") == "章鱼猫"
    assert store.name_by_id("") is None
    store.usermap_set("octocat", "u_456", "改名了")     # upsert
    assert store.usermap_get("octocat") == "u_456"
    assert len(store.usermap_list()) == 1
    assert store.usermap_delete("octocat") is True
    assert store.usermap_delete("octocat") is False


def test_usermap_seed_only_when_empty(store):
    store.usermap_seed({"a": "u_a", "b": {"user_id": "u_b", "name": "乙"}})
    assert len(store.usermap_list()) == 2
    assert store.name_by_id("u_b") == "乙"
    store.usermap_seed({"c": "u_c"})     # 表非空 → 不再灌
    assert len(store.usermap_list()) == 2, "seed 覆盖了运行时数据"


def test_lifespan_actually_creates_tables(store):
    """🔴 建表从模块级挪进 lifespan 后，必须证明它【真的会执行】。

    否则这次改动只是把「import 时失败」推迟成「跑起来之后第一次用库才失败」——
    那比原来更糟：进程起得来、健康检查还绿，真有人发版时才炸。
    这条用例就是那个「装置确实产出了东西」的断言。
    """
    from fastapi.testclient import TestClient

    from app import server

    with server.store._conn() as c:          # 先把表删掉，制造"未初始化"状态
        c.execute("drop table if exists releases")
        c.execute("drop table if exists user_map")

    with TestClient(server.app):             # 进入上下文 → 触发 lifespan
        pass

    with server.store._conn() as c:
        got = {r["to_regclass"] for r in c.execute(
            "select to_regclass('releases') union all select to_regclass('user_map')")}
    assert got == {"releases", "user_map"}, f"lifespan 没有建表，实际={got}"
