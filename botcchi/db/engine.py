"""异步数据库引擎封装。

schema 由 create_all 建立；等 M3 前 schema 稳定后引入 Alembic 迁移。
create_all 不会给已有表补新列，_ADDED_COLUMNS 记录事后追加的列并在
init 时按需 ALTER TABLE（SQLite 与 PostgreSQL 语法一致的子集）。
"""

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("social_users", "pendant", "VARCHAR(32)"),
    ("game_escrows", "coins", "INTEGER NOT NULL DEFAULT 0"),
]


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for table, column, ddl_type in _ADDED_COLUMNS:
                names = await conn.run_sync(
                    lambda sc, t=table: [
                        c["name"] for c in inspect(sc).get_columns(t)
                    ]
                )
                if column not in names:
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
                    )

    def session(self) -> AsyncSession:
        return self.session_factory()

    async def aclose(self) -> None:
        await self.engine.dispose()
