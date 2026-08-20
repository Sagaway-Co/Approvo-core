"""instance_code -> 发版规格 的持久化映射(PostgreSQL)。

为什么要存:审批通过的事件里只有 instance_code,得反查「这个审批对应哪个 repo/tag/集群、
该怎么部署」。顺带记状态防重复部署(审批事件可能重复投递)。

🔴 2026-08-10 从 SQLite 迁到 PostgreSQL。迁移的三条硬约束:
  1. 所有函数签名【保持不变】—— server.py / events.py 依赖它们,这次只换存储不改语义。
  2. try_claim_for_deploy 的【原子性】不能丢:它是"防重复部署"的唯一保证。
     SQLite 版靠单文件 + 进程内锁;PG 版靠单条 UPDATE ... WHERE status='pending' 的
     行级锁 —— 数据库保证同一行只有一个事务能改成 deploying,比进程内锁更强
     (进程内锁跨副本无效,PG 的不是)。
  3. 时间列【继续用 text 存 ISO 8601】而不是 timestamptz:
     现有 304 条记录就是这个格式,ISO 8601 的字典序等于时间序,
     所有 order by created_at/updated_at 的行为与迁移前【逐字节一致】。
     换类型能更规范,但会引入"排序行为是否变了"这个需要重新验证的问题,
     而这次迁移的目标是【零语义变化】。要改留到单独一轮。
"""
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import settings

# 连接池:approvo 负载极小(实测 4m CPU),但它是【长期运行】的服务,
# 必须能扛住 RDS 重启/网络抖动 —— 池会自动重建坏连接,裸连接不会。
_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ConnectionPool:
    """懒初始化连接池(双重检查加锁)。

    🔴 这里必须加锁:approvo 是【多线程】的 —— 部署丢线程、只读签发丢线程、
    启动对账/部署/只读签发各自独立线程、uvicorn 还会并发处理请求。无锁的懒初始化会让两个线程
    同时看到 _pool is None 各建一个池,被覆盖的那个【已经建好连接却永远不会关闭】,
    形成连接泄漏;而 RDS 的连接数是有上限的。
    概率低不等于不会发生,且它只会在启动瞬间偶发 —— 这种 bug 最难查。
    """
    global _pool
    if _pool is None:                      # 快路径:已初始化则完全不进锁
        with _pool_lock:
            if _pool is None:              # 双重检查:等锁期间可能已被别的线程建好
                if not settings.DB_DSN:
                    raise RuntimeError(
                        "DB_DSN 未配置:approvo 的状态存 PostgreSQL,没有它无法工作")
                # 🔴 open 必须显式传:psycopg_pool 已警告该参数的默认值【将来会变成 False】。
                # 依赖默认值 = 某次依赖升级后池不再自动打开,而这只会在运行时暴露。
                _pool = ConnectionPool(
                    settings.DB_DSN, min_size=1, max_size=4, timeout=10, open=True,
                    kwargs={"row_factory": dict_row},
                )
    return _pool


@contextmanager
def _conn():
    """借一条连接;退出时自动 commit(异常则 rollback),与原 sqlite3 的 with 语义一致。"""
    with _get_pool().connection() as c:
        yield c


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init():
    with _conn() as c:
        c.execute(
            """create table if not exists releases(
                instance_code text primary key,
                repo text, tag text, image text,
                cluster text, namespace text,
                spec_json text,
                status text,
                created_at text, updated_at text)"""
        )
        c.execute(
            """create table if not exists user_map(
                github_login text primary key,
                user_id text,
                name text,
                updated_at text)"""
        )
        # 这两个索引 SQLite 版没有(单文件全表扫也够快),PG 上给高频查询补上:
        # list_pending 启动对账时查一次;last_success_commit 每次发版都查。
        c.execute("create index if not exists idx_releases_status on releases(status)")
        c.execute("create index if not exists idx_releases_repo_tag on releases(repo, tag)")


def save(instance_code: str, spec: dict, status: str = "pending"):
    """等价于原 SQLite 的 insert or replace(整行替换,含 created_at)。"""
    now = _now()
    with _conn() as c:
        c.execute(
            """insert into releases (instance_code, repo, tag, image, cluster, namespace,
                                     spec_json, status, created_at, updated_at)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (instance_code) do update set
                 repo=excluded.repo, tag=excluded.tag, image=excluded.image,
                 cluster=excluded.cluster, namespace=excluded.namespace,
                 spec_json=excluded.spec_json, status=excluded.status,
                 created_at=excluded.created_at, updated_at=excluded.updated_at""",
            (instance_code, spec["repo"], spec["tag"], spec["image"],
             spec["cluster"], spec["namespace"], json.dumps(spec, ensure_ascii=False),
             status, now, now),
        )


def get(instance_code: str) -> dict | None:
    with _conn() as c:
        row = c.execute("select * from releases where instance_code=%s",
                        (instance_code,)).fetchone()
        return dict(row) if row else None


def set_status(instance_code: str, status: str):
    with _conn() as c:
        c.execute("update releases set status=%s, updated_at=%s where instance_code=%s",
                  (status, _now(), instance_code))


def status_history(repo: str, tag: str, stage: str | None = None) -> list[str]:
    """同 repo+tag(+stage) 的全部历史状态(新→旧),用于判断重复提交还是被拒后重发。

    🔴 为什么必须把 stage 算进去:
    普通发版的 tag 天然带环境(V1.2.3-pre / -release),所以 repo+tag 判重够用。
    但【同一个 tag 要发到两个环境】的通道不是这样:
      · 例如"部署仓的数据库迁移"这类条目,tag 是部署仓的 commit sha —— 两个环境完全相同
      · 于是 QA 应用成功后,提交生产会被判成"already deployed"而静默跳过
    实测:同一个迁移文件先发 QA 成功,再发生产直接返回 {"skipped":"already deployed"},
    生产【根本没执行】,而返回码是 200 —— 看起来像成功。这类"静默跳过"比报错危险得多。

    stage=None 时退化为旧行为(按 repo+tag),保持既有调用兼容。
    """
    sql = ("select status from releases where repo=%s and tag=%s"
           + (" and spec_json::jsonb->>'stage' = %s" if stage else "")
           + " order by created_at desc")
    args = (repo, tag, stage) if stage else (repo, tag)
    with _conn() as c:
        return [r["status"] for r in c.execute(sql, args).fetchall()]


TAG_SHA_RE = re.compile(r"\.([0-9a-f]{7,40})$")   # 镜像 tag 约定 vYYYYMMDD.<short_sha>


def last_success_commit(repo: str, stage: str | None = None) -> str | None:
    """该应用最近一次部署成功的 commit,作变更清单对比的 base。
    只有 gate 知道线上真正部署到了哪个版本(构建≠部署,中间可能有被拒的)。

    🔴 必须能按 stage 取(同族第三处):基线要取【同环境】的上次成功。
    否则"先发 QA 再发生产"时,生产的基线会取到刚才那次 QA 成功,
    于是卡片显示"与已部署版本相比无新增提交" —— 而生产其实要装一个全新版本。
    普通发版的 tag 天然带环境所以不明显;同一 tag 发两个环境的通道会直接暴露。
    """
    cond = " and spec_json::jsonb->>'stage' = %s" if stage else ""
    args = (repo, stage) if stage else (repo,)
    with _conn() as c:
        rows = c.execute(
            "select spec_json from releases where repo=%s and status='success'"
            + cond + " order by updated_at desc limit 5", args).fetchall()
    for r in rows:   # 部分 CI 没传 commit,从 tag 末段抽短 sha 兜底;都没有再往前看
        spec = json.loads(r["spec_json"]) or {}
        if spec.get("commit"):
            return spec["commit"]
        m = TAG_SHA_RE.search(spec.get("tag") or "")
        if m:
            return m.group(1)
    return None


# ---------- github 用户名 -> 飞书 user_id 映射(运行时可改,/admin 维护)----------

def last_success_at(repo: str, stage: str | None = None) -> str | None:
    """该应用最近一次【部署成功】的时间,用作"数据库迁移变更"的时间基线。

    与 last_success_commit 同源:只有 gate 知道线上真正部署到了哪个版本/什么时候。
    迁移文件在部署仓而非业务仓,没有 commit 可比,只能用时间口径 ——
    卡片里必须如实写明这一点,不能让审批人误以为它等于"数据库里未应用的迁移"。

    🔴 同样必须能按 stage 取(同族第四处):否则"先 QA 再生产"时,生产会以
    刚才那次 QA 成功为基线,把本次真实的迁移新增显示成"无变更"—— 谎报安全。
    """
    cond = " and spec_json::jsonb->>'stage' = %s" if stage else ""
    args = (repo, stage) if stage else (repo,)
    with _conn() as c:
        row = c.execute(
            "select updated_at from releases where repo=%s and status='success'"
            + cond + " order by updated_at desc limit 1", args).fetchone()
    return row["updated_at"] if row else None


def usermap_list() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "select github_login, user_id, name, updated_at from user_map "
            "order by github_login").fetchall()
        return [dict(r) for r in rows]


def usermap_get(github_login: str) -> str | None:
    with _conn() as c:
        row = c.execute("select user_id from user_map where github_login=%s",
                        (github_login,)).fetchone()
        return row["user_id"] if row else None


def usermap_get_row(github_login: str) -> dict | None:
    with _conn() as c:
        row = c.execute("select github_login, user_id, name from user_map where github_login=%s",
                        (github_login,)).fetchone()
        return dict(row) if row else None


def name_by_id(uid: str) -> str | None:
    """按 user_id/open_id 反查真名(用于显示审批人)。"""
    if not uid:
        return None
    with _conn() as c:
        row = c.execute("select name from user_map where user_id=%s", (uid,)).fetchone()
        return row["name"] if row and row["name"] else None


def list_pending() -> list[str]:
    with _conn() as c:
        return [r["instance_code"] for r in
                c.execute("select instance_code from releases where status='pending'").fetchall()]


def usermap_set(github_login: str, user_id: str, name: str = ""):
    with _conn() as c:
        c.execute(
            """insert into user_map (github_login, user_id, name, updated_at)
               values (%s,%s,%s,%s)
               on conflict (github_login) do update set
                 user_id=excluded.user_id, name=excluded.name, updated_at=excluded.updated_at""",
            (github_login, user_id, name, _now()))


def usermap_delete(github_login: str) -> bool:
    with _conn() as c:
        cur = c.execute("delete from user_map where github_login=%s", (github_login,))
        return cur.rowcount == 1


def usermap_seed(config_map: dict):
    """首次启动(表为空)时用 config.user_map 灌初始值;之后以 DB 为准,不再覆盖。

    支持两种值格式:
      login: "<user_id>"                       # 简单格式(老),name 为空
      login: {user_id: "<uid>", name: "真名"}   # 扩展格式(推荐),含真名
    """
    if not config_map:
        return
    with _conn() as c:
        n = c.execute("select count(*) as c from user_map").fetchone()["c"]
        if n:
            return
        now = _now()
        for login, val in config_map.items():
            if isinstance(val, dict):
                uid, name = val.get("user_id", ""), val.get("name", "")
            else:
                uid, name = str(val), ""
            c.execute("insert into user_map (github_login, user_id, name, updated_at) "
                      "values (%s,%s,%s,%s) on conflict (github_login) do nothing",
                      (login, uid, name, now))


def try_claim_for_deploy(instance_code: str) -> bool:
    """原子地把 pending 翻成 deploying。返回 True 表示本次抢到了,可以去部署;
    False 表示别的事件已抢走(防重复部署)。

    🔴 这是全系统防重复部署的唯一保证。原子性来自【单条 UPDATE 的行级锁】:
    并发的两个事务里只有一个能把该行从 pending 改走,另一个 rowcount=0。
    不要拆成"先 select 再 update",那会重新引入竞态。
    """
    with _conn() as c:
        cur = c.execute(
            "update releases set status='deploying', updated_at=%s "
            "where instance_code=%s and status='pending'",
            (_now(), instance_code),
        )
        return cur.rowcount == 1
