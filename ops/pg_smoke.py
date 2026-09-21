# -*- coding: utf-8 -*-
"""PostgreSQL 后端端到端冒烟（P6）。

用法：
  py ops/pg_smoke.py --dsn postgresql://dnatest:dnatest@127.0.0.1:5433/dnamemory_smoke \
     [--src ops/data/lccc_memory.db]

流程：重建独立库 →（可选）从 SQLite 迁移 → 经 HTTP 层读/写/治理 →
      关闭应用 → 校验 PG 连接归零。

默认库名带 ``_smoke`` 后缀并会 **DROP/CREATE** —— 不要指向主存储库。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))   # server/ 包在仓库根，不在 src/

DEFAULT_DSN = "postgresql://dnatest:dnatest@127.0.0.1:5433/dnamemory_smoke"


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _split_dsn(dsn: str):
    """返回 (管理库 DSN, 库名)。"""
    head, _, db = dsn.rpartition("/")
    return f"{head}/postgres", db.split("?")[0]


def _fresh_db(dsn: str):
    import psycopg
    admin, db = _split_dsn(dsn)
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{db}"')
        c.execute(f'CREATE DATABASE "{db}"')
    return db


def _active_connections(dsn: str) -> int:
    import psycopg
    admin, db = _split_dsn(dsn)
    with psycopg.connect(admin, autocommit=True) as c:
        return c.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (db,)).fetchone()[0]


def main():
    _force_utf8()
    p = argparse.ArgumentParser(description="PostgreSQL 后端端到端冒烟")
    p.add_argument("--dsn", default=DEFAULT_DSN)
    p.add_argument("--src", default=None,
                   help="可选：先从这个 SQLite 库迁移数据")
    p.add_argument("--db-name-guard", default="_smoke",
                   help="库名必须含该子串才允许 DROP/CREATE（防误指主库）")
    args = p.parse_args()

    if args.db_name_guard not in args.dsn.rpartition("/")[2]:
        sys.exit(f"拒绝执行：DSN 库名不含 {args.db_name_guard!r}，"
                 f"本脚本会 DROP/CREATE 目标库。")

    ok = True
    print("1) 重建独立冒烟库")
    db = _fresh_db(args.dsn)
    print(f"   库 {db} 已重建")

    if args.src:
        print(f"2) 从 {args.src} 迁移")
        rc = subprocess.run(
            [sys.executable, str(ROOT / "ops" / "migrate_sqlite_to_pg.py"),
             "--src", args.src, "--dsn", args.dsn, "--truncate"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        tail = [l for l in (rc.stdout or "").splitlines() if l.strip()][-1:]
        print(f"   {tail[0] if tail else '(无输出)'}")
        if rc.returncode != 0:
            print(rc.stdout, rc.stderr)
            sys.exit("迁移失败")
    else:
        print("2) 跳过迁移（未提供 --src）")

    print("3) HTTP 层读写与治理")
    from fastapi.testclient import TestClient

    from server.api.app import create_app

    H = {"Authorization": "Bearer smoke"}
    app = create_app(path=":memory:", dsn=args.dsn, token="smoke")
    with TestClient(app) as client:
        stats = client.get("/stats", headers=H).json()
        print(f"   /stats          nodes={stats['nodes']} facts={stats['facts']} "
              f"edges={stats['edges']}")

        e = client.post("/entities", json={"name": "冒烟实体"},
                        headers=H).json()
        f = client.post("/facts", json={"entity": "冒烟实体", "key": "city",
                                        "value": "上海"}, headers=H).json()
        print(f"   写入 entity/fact id={e.get('id')}/{f.get('id')}")

        hit = client.get("/facts", params={"entity": "冒烟实体"}, headers=H).json()
        got = hit["items"][0]["value"] if hit.get("items") else None
        print(f"   回读 /facts     value={got!r}")
        ok &= got == "上海"

        ctx = client.post("/context", json={"text": "冒烟实体住哪"},
                          headers=H).json()
        print(f"   /context        区块数={len(ctx)}")
        ok &= len(ctx) >= 16

        rc2 = client.post("/extract-patterns", json={}, headers=H)
        print(f"   治理 /extract-patterns status={rc2.status_code}")
        ok &= rc2.status_code == 200

        n_conn_app = _active_connections(args.dsn)
        print(f"   应用运行中连接数={n_conn_app}")

    print("4) 关闭应用后校验连接归零")
    n_conn = _active_connections(args.dsn)
    print(f"   残留连接={n_conn}")
    ok &= n_conn == 0

    print()
    print("✓ 冒烟通过" if ok else "✗ 冒烟失败")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
