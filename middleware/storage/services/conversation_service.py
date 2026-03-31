"""Conversation service for managing conversations within sessions."""

from __future__ import annotations

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from middleware.storage.models import Conversation, Session
from middleware.storage.schemas import (
    ConversationCreate,
    ConversationRead,
    ConversationUpdate,
)
from middleware.storage.utils import get_east_8_time
from .base_service import BaseService
from .sqlite_fallback import connect, dumps_json, loads_json, new_id


class ConversationService(BaseService):
    """Service for conversation CRUD operations."""

    async def create(self, data: ConversationCreate) -> ConversationRead:
        """Create a new conversation. Auto-assigns sequence if not provided."""
        return self._create_sqlite(data)

    async def get(self, conversation_id: str) -> ConversationRead | None:
        """Get conversation by ID."""
        return self._get_sqlite(conversation_id)

    async def update(
        self, conversation_id: str, data: ConversationUpdate
    ) -> ConversationRead | None:
        """Update conversation."""
        return self._update_sqlite(conversation_id, data)

    async def delete(self, conversation_id: str) -> bool:
        """Delete conversation."""
        return self._delete_sqlite(conversation_id)

    async def list_by_session(
        self, session_id: str, limit: int = 1000
    ) -> list[ConversationRead]:
        """List conversations by session, ordered by sequence."""
        return self._list_by_session_sqlite(session_id, limit)

    async def get_next_sequence(self, session_id: str) -> int:
        """Get next sequence number for a session."""
        return self._get_next_sequence_sqlite(session_id)

    @staticmethod
    def _row_to_read(row) -> ConversationRead:
        return ConversationRead(
            id=row["id"],
            session_id=row["session_id"],
            sequence=row["sequence"],
            role=row["role"],
            title=row["title"],
            content=row["content"],
            content_detail=loads_json(row["content_detail"], None),
            tool_calls=loads_json(row["tool_calls"], None),
            tool_call_id=row["tool_call_id"],
            meta_info=loads_json(row["meta_info"], {}),
            tokens=row["tokens"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _create_sqlite(self, data: ConversationCreate) -> ConversationRead:
        now = get_east_8_time().isoformat()
        conversation_id = new_id()
        with connect() as conn:
            sequence = data.sequence or self._get_next_sequence_sqlite(
                data.session_id, conn=conn
            )
            conn.execute(
                """
                INSERT INTO conversations (
                    id, session_id, sequence, role, title, content,
                    content_detail, tool_calls, tool_call_id, meta_info,
                    tokens, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    data.session_id,
                    sequence,
                    data.role,
                    data.title,
                    data.content,
                    dumps_json(data.content_detail)
                    if data.content_detail is not None
                    else None,
                    dumps_json(data.tool_calls) if data.tool_calls is not None else None,
                    data.tool_call_id,
                    dumps_json(data.meta_info),
                    data.tokens,
                    now,
                    now,
                ),
            )
            self._update_session_stats_sqlite(data.session_id, conn)
            conn.commit()
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return self._row_to_read(row)

    def _get_sqlite(self, conversation_id: str) -> ConversationRead | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return self._row_to_read(row) if row else None

    def _update_sqlite(
        self, conversation_id: str, data: ConversationUpdate
    ) -> ConversationRead | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if row is None:
                return None

            updated_at = get_east_8_time().isoformat()
            conn.execute(
                """
                UPDATE conversations
                SET title = ?, content = ?, meta_info = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    data.title if data.title is not None else row["title"],
                    data.content if data.content is not None else row["content"],
                    dumps_json(data.meta_info)
                    if data.meta_info is not None
                    else row["meta_info"],
                    updated_at,
                    conversation_id,
                ),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return self._row_to_read(updated)

    def _delete_sqlite(self, conversation_id: str) -> bool:
        with connect() as conn:
            row = conn.execute(
                "SELECT session_id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            self._update_session_stats_sqlite(row["session_id"], conn)
            conn.commit()
        return True

    def _list_by_session_sqlite(
        self, session_id: str, limit: int
    ) -> list[ConversationRead]:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM conversations
                WHERE session_id = ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [self._row_to_read(row) for row in rows]

    def _get_next_sequence_sqlite(
        self, session_id: str, conn=None
    ) -> int:
        owns_conn = conn is None
        if conn is None:
            conn = connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS max_seq FROM conversations WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return int(row["max_seq"]) + 1
        finally:
            if owns_conn:
                conn.close()

    def _update_session_stats_sqlite(self, session_id: str, conn) -> None:
        stats = conn.execute(
            """
            SELECT COUNT(id) AS count, COALESCE(SUM(tokens), 0) AS tokens
            FROM conversations
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE sessions
            SET conversation_count = ?, total_tokens = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                stats["count"],
                stats["tokens"],
                get_east_8_time().isoformat(),
                session_id,
            ),
        )

    async def _create(
        self, db: AsyncSession, data: ConversationCreate
    ) -> ConversationRead:
        """Create conversation implementation."""
        # Auto-assign sequence if not provided
        sequence = data.sequence
        if sequence is None:
            sequence = await self._get_next_sequence(db, data.session_id)

        obj = Conversation(
            session_id=data.session_id,
            sequence=sequence,
            role=data.role,
            title=data.title,
            content=data.content,
            content_detail=data.content_detail,
            tool_calls=data.tool_calls,
            tool_call_id=data.tool_call_id,
            meta_info=data.meta_info,
            tokens=data.tokens,
            created_at=get_east_8_time(),
            updated_at=get_east_8_time(),
        )
        db.add(obj)

        # Flush first so aggregate queries include this new conversation
        await db.flush()

        # Update session stats
        await self._update_session_stats(db, data.session_id)

        await db.commit()
        await db.refresh(obj)
        return ConversationRead.model_validate(obj)

    async def _get(
        self, db: AsyncSession, conversation_id: str
    ) -> ConversationRead | None:
        """Get conversation implementation."""
        obj = await db.get(Conversation, conversation_id)
        if obj is None:
            return None
        return ConversationRead.model_validate(obj)

    async def _update(
        self, db: AsyncSession, conversation_id: str, data: ConversationUpdate
    ) -> ConversationRead | None:
        """Update conversation implementation."""
        obj = await db.get(Conversation, conversation_id)
        if obj is None:
            return None

        if data.title is not None:
            obj.title = data.title
        if data.content is not None:
            obj.content = data.content
        if data.meta_info is not None:
            obj.meta_info = data.meta_info

        # Update timestamp to East 8 time
        obj.updated_at = get_east_8_time()

        db.add(obj)
        await db.commit()
        await db.refresh(obj)
        return ConversationRead.model_validate(obj)

    async def _delete(self, db: AsyncSession, conversation_id: str) -> bool:
        """Delete conversation implementation."""
        obj = await db.get(Conversation, conversation_id)
        if obj is None:
            return False

        session_id = obj.session_id
        await db.delete(obj)

        # Update session stats
        await self._update_session_stats(db, session_id)

        await db.commit()
        return True

    async def _list_by_session(
        self, db: AsyncSession, session_id: str, limit: int
    ) -> list[ConversationRead]:
        """List by session implementation."""
        stmt = (
            select(Conversation)
            .where(Conversation.session_id == session_id)
            .order_by(Conversation.sequence.asc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        return [ConversationRead.model_validate(x) for x in rows]

    async def _get_next_sequence(self, db: AsyncSession, session_id: str) -> int:
        """Get next sequence number for a session."""
        stmt = select(func.max(Conversation.sequence)).where(
            Conversation.session_id == session_id
        )
        result = await db.execute(stmt)
        max_seq = result.scalar()
        return (max_seq or 0) + 1

    async def _update_session_stats(self, db: AsyncSession, session_id: str) -> None:
        """Update session conversation count and total tokens."""
        # Get stats
        stmt = select(
            func.count(Conversation.id).label("count"),
            func.coalesce(func.sum(Conversation.tokens), 0).label("tokens"),
        ).where(Conversation.session_id == session_id)
        result = await db.execute(stmt)
        stats = result.one()

        # Update session
        session = await db.get(Session, session_id)
        if session:
            session.conversation_count = stats.count
            session.total_tokens = stats.tokens
            db.add(session)
