#!/usr/bin/env python3
"""将旧版 PostgreSQL dump 中的金币/好感度迁移到 Botcchi。

用法示例（目标数据库需已启动，且已由 Botcchi 建好 social_users 表）：

    uv run python scripts/migrate_legacy_social.py \
      --dump data/bocchi.dump \
      --target-url postgresql+asyncpg://botcchi:botcchi@localhost:5432/botcchi

脚本会在同一 PostgreSQL 实例创建一个临时数据库恢复 dump，只读取
sign_users、user_console、friend_users 三张旧表，完成后自动删除临时数据库。
重复执行是安全的：同一 user_id 的金币、好感度和签到次数会被旧值覆盖。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import subprocess
from urllib.parse import urlsplit

import asyncpg


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dump", default="data/bocchi.dump")
    p.add_argument(
        "--target-url",
        default="postgresql+asyncpg://botcchi:botcchi@localhost:5432/botcchi",
    )
    p.add_argument(
        "--admin-url",
        help="用于 CREATE/DROP DATABASE 的连接串；默认使用目标库同一实例的 postgres 库",
    )
    return p.parse_args()


def _pg_parts(url: str, default_db: str) -> tuple[str, dict[str, str]]:
    parsed = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    if not parsed.hostname:
        raise ValueError(f"无效的 PostgreSQL URL: {url}")
    opts = {
        "host": parsed.hostname,
        "port": str(parsed.port or 5432),
        "user": parsed.username or "postgres",
        "database": parsed.path.lstrip("/") or default_db,
    }
    if parsed.password is not None:
        opts["password"] = parsed.password
    return f"postgresql://{opts['user']}@{opts['host']}:{opts['port']}/{opts['database']}", opts


async def main() -> None:
    args = _args()
    _, target = _pg_parts(args.target_url, "botcchi")
    if args.admin_url:
        _, admin = _pg_parts(args.admin_url, "postgres")
    else:
        admin = {**target, "database": "postgres"}

    temp_db = f"botcchi_legacy_{secrets.token_hex(6)}"
    admin_conn = await asyncpg.connect(**admin)
    try:
        await admin_conn.execute(f'CREATE DATABASE "{temp_db}"')
    finally:
        await admin_conn.close()

    restore = {
        "host": admin["host"],
        "port": admin["port"],
        "user": admin["user"],
        "database": temp_db,
    }
    if admin.get("password"):
        restore["password"] = admin["password"]
    env = os.environ.copy()
    if admin.get("password"):
        env["PGPASSWORD"] = admin["password"]
    cmd = [
        "pg_restore",
        "--no-owner",
        "--no-privileges",
        "--clean",
        "--if-exists",
        "--table=sign_users",
        "--table=user_console",
        "--table=friend_users",
        "-h",
        restore["host"],
        "-p",
        restore["port"],
        "-U",
        restore["user"],
        "-d",
        temp_db,
        args.dump,
    ]
    try:
        subprocess.run(cmd, env=env, check=True)
        source = await asyncpg.connect(**restore)
        target_conn = await asyncpg.connect(**target)
        try:
            rows = await source.fetch(
                """
                SELECT COALESCE(s.user_id, u.user_id) AS user_id,
                       COALESCE(s.impression, 0)::double precision AS impression,
                       COALESCE(s.sign_count, 0) AS sign_count,
                       COALESCE(u.gold, 0) AS coins,
                       COALESCE(f.user_name, f.nickname, '') AS user_name
                FROM user_console u
                FULL OUTER JOIN sign_users s ON u.id = s.user_console_id
                LEFT JOIN LATERAL (
                    SELECT user_name, nickname FROM friend_users f
                    WHERE f.user_id = COALESCE(s.user_id, u.user_id)
                    ORDER BY f.id DESC LIMIT 1
                ) f ON true
                """
            )
            async with target_conn.transaction():
                for row in rows:
                    await target_conn.execute(
                        """
                        INSERT INTO social_users
                            (user_id, user_name, impression, coins, sign_count,
                             streak, created_at)
                        VALUES ($1, $2, $3, $4, $5, 0, now())
                        ON CONFLICT (user_id) DO UPDATE SET
                            user_name = EXCLUDED.user_name,
                            impression = EXCLUDED.impression,
                            coins = EXCLUDED.coins,
                            sign_count = EXCLUDED.sign_count
                        """,
                        str(row["user_id"]), row["user_name"] or "",
                        row["impression"], row["coins"], row["sign_count"],
                    )
            print(f"已迁移 {len(rows)} 个用户的金币/好感度")
        finally:
            await source.close()
            await target_conn.close()
    finally:
        cleanup = await asyncpg.connect(**admin)
        try:
            await cleanup.execute(f'DROP DATABASE IF EXISTS "{temp_db}"')
        finally:
            await cleanup.close()


if __name__ == "__main__":
    asyncio.run(main())
