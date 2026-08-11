"""把 approvo 的状态从 SQLite 迁到 PostgreSQL(幂等,可重跑)。

用法:
    DB_PATH=/data/release.db \
    DB_DSN=postgresql://user:pass@host:5432/approvo \
    python -m scripts.migrate_sqlite_to_pg [--dry-run]

🔴 验收判据是【两边数据逐行一致】,不是【脚本返回 0】。
   所以迁完会算两边的逐行指纹并比对,不一致就非零退出 ——
   "跑完没报错"这种判据太弱:漏迁一张表、少迁几行、字段错位,都不会报错。

🔴 幂等:用 upsert,重跑不会产生重复行,也不会丢失重跑期间的新数据。
"""
import hashlib
import os
import sqlite3
import sys

import psycopg
from psycopg.rows import dict_row

REL_COLS = ["instance_code", "repo", "tag", "image", "cluster", "namespace",
            "spec_json", "status", "created_at", "updated_at"]
MAP_COLS = ["github_login", "user_id", "name", "updated_at"]


def _fingerprint(rows, cols, key):
    """对整表算一个指纹:按主键排序后把所有字段喂进 sha256。
    只比条数是不够的 —— 条数对但内容错位同样是迁移失败。"""
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda x: str(x[key])):
        for c in cols:
            v = r[c]
            h.update((("" if v is None else str(v)) + "\x00").encode())
    return h.hexdigest()[:16]


def main():
    dry = "--dry-run" in sys.argv
    src, dsn = os.environ.get("DB_PATH", ""), os.environ.get("DB_DSN", "")
    if not src or not os.path.exists(src):
        sys.exit(f"找不到源 SQLite: {src!r}")
    if not dsn:
        sys.exit("DB_DSN 未设置")

    sq = sqlite3.connect(src)
    sq.row_factory = sqlite3.Row
    rel = [dict(r) for r in sq.execute(f"select {','.join(REL_COLS)} from releases")]
    ump = [dict(r) for r in sq.execute(f"select {','.join(MAP_COLS)} from user_map")]
    print(f"源(SQLite): releases={len(rel)} user_map={len(ump)}")
    # 迁移前先把源的状态分布打出来 —— 便于事后核对"卡在 deploying 的历史"有没有被带过去
    dist = {}
    for r in rel:
        dist[r["status"]] = dist.get(r["status"], 0) + 1
    print(f"源状态分布: {dist}")

    if dry:
        print("--dry-run:不写入,退出")
        return

    with psycopg.connect(dsn, row_factory=dict_row) as pg:
        pg.execute("""create table if not exists releases(
            instance_code text primary key, repo text, tag text, image text,
            cluster text, namespace text, spec_json text, status text,
            created_at text, updated_at text)""")
        pg.execute("""create table if not exists user_map(
            github_login text primary key, user_id text, name text, updated_at text)""")
        pg.execute("create index if not exists idx_releases_status on releases(status)")
        pg.execute("create index if not exists idx_releases_repo_tag on releases(repo, tag)")

        for r in rel:
            pg.execute(
                f"insert into releases ({','.join(REL_COLS)}) "
                f"values ({','.join(['%s'] * len(REL_COLS))}) "
                "on conflict (instance_code) do update set " +
                ", ".join(f"{c}=excluded.{c}" for c in REL_COLS if c != "instance_code"),
                [r[c] for c in REL_COLS])
        for m in ump:
            pg.execute(
                f"insert into user_map ({','.join(MAP_COLS)}) "
                f"values ({','.join(['%s'] * len(MAP_COLS))}) "
                "on conflict (github_login) do update set " +
                ", ".join(f"{c}=excluded.{c}" for c in MAP_COLS if c != "github_login"),
                [m[c] for c in MAP_COLS])
        pg.commit()

        # ── 对账:判据在这里,不在上面 ──
        pg_rel = pg.execute(f"select {','.join(REL_COLS)} from releases").fetchall()
        pg_ump = pg.execute(f"select {','.join(MAP_COLS)} from user_map").fetchall()

    ok = True
    for name, a, b, cols, key in [
        ("releases", rel, pg_rel, REL_COLS, "instance_code"),
        ("user_map", ump, pg_ump, MAP_COLS, "github_login"),
    ]:
        fa, fb = _fingerprint(a, cols, key), _fingerprint(b, cols, key)
        same = len(a) == len(b) and fa == fb
        ok = ok and same
        print(f"{'✅' if same else '❌'} {name}: sqlite={len(a)}({fa}) pg={len(b)}({fb})")

    if not ok:
        sys.exit("❌ 迁移后两边数据不一致,请勿切流量")
    print("✅ 逐行指纹一致,迁移可信")


if __name__ == "__main__":
    main()
