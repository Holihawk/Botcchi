"""数据库背书的会话存储与人格绑定"""

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..agent.messages import ChatMessage
from ..channels.base import SessionKey
from ..memory.short_term import ConversationStore
from .engine import Database
from .models import ConversationRow, MessageRow, PersonaBindingRow, utcnow


class DBConversationStore(ConversationStore):
    def __init__(self, db: Database, max_messages: int = 40):
        self.db = db
        self.max_messages = max_messages

    async def get(self, session: SessionKey) -> list[ChatMessage]:
        async with self.db.session() as s:
            conv = await s.scalar(
                select(ConversationRow).where(
                    ConversationRow.session_key == str(session)
                )
            )
            if conv is None:
                return []
            stmt = (
                select(MessageRow)
                .where(MessageRow.conversation_id == conv.id)
                .order_by(MessageRow.id.desc())
                .limit(self.max_messages)
            )
            if conv.reset_at is not None:
                stmt = stmt.where(MessageRow.created_at > conv.reset_at)
            rows = list((await s.scalars(stmt)).all())
        rows.reverse()
        return [
            ChatMessage(role=row.role, content=row.content, reasoning=row.reasoning)
            for row in rows
        ]

    async def append(self, session: SessionKey, *messages: ChatMessage) -> None:
        async with self.db.session() as s:
            conv = await s.scalar(
                select(ConversationRow).where(
                    ConversationRow.session_key == str(session)
                )
            )
            if conv is None:
                conv = ConversationRow(session_key=str(session))
                s.add(conv)
                await s.flush()
            s.add_all(
                MessageRow(
                    conversation_id=conv.id,
                    role=m.role,
                    content=m.text(),
                    reasoning=m.reasoning,
                )
                for m in messages
            )
            await s.commit()

    async def reset(self, session: SessionKey) -> None:
        async with self.db.session() as s:
            conv = await s.scalar(
                select(ConversationRow).where(
                    ConversationRow.session_key == str(session)
                )
            )
            if conv is not None:
                conv.reset_at = utcnow()
                await s.commit()


class PersonaBindingStore:
    """会话 → Assistant 绑定"""

    def __init__(self, db: Database):
        self.db = db

    async def get(self, session: SessionKey) -> str | None:
        async with self.db.session() as s:
            return await s.scalar(
                select(PersonaBindingRow.assistant).where(
                    PersonaBindingRow.session_key == str(session)
                )
            )

    async def set(self, session: SessionKey, assistant: str) -> None:
        async with self.db.session() as s:
            dialect = s.bind.dialect.name if s.bind is not None else "postgresql"
            insert = sqlite_insert if dialect == "sqlite" else pg_insert
            stmt = insert(PersonaBindingRow).values(
                session_key=str(session), assistant=assistant
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[PersonaBindingRow.session_key],
                set_={"assistant": assistant},
            )
            await s.execute(stmt)
            await s.commit()

    async def clear(self, session: SessionKey) -> None:
        async with self.db.session() as s:
            await s.execute(
                delete(PersonaBindingRow).where(
                    PersonaBindingRow.session_key == str(session)
                )
            )
            await s.commit()


class InMemoryPersonaBindingStore:
    """无数据库时的进程内实现"""

    def __init__(self) -> None:
        self._data: dict[SessionKey, str] = {}

    async def get(self, session: SessionKey) -> str | None:
        return self._data.get(session)

    async def set(self, session: SessionKey, assistant: str) -> None:
        self._data[session] = assistant

    async def clear(self, session: SessionKey) -> None:
        self._data.pop(session, None)
