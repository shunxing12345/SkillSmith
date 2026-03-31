"""Session service for managing chat sessions."""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from middleware.storage.models import Session, SessionStatus
from middleware.storage.schemas import SessionCreate, SessionRead, SessionUpdate
from middleware.storage.utils import get_east_8_time
from .base_service import BaseService
from .sqlite_fallback import connect, dumps_json, loads_json, new_id


class SessionService(BaseService):
    """Service for managing chat sessions with automatic database session management."""

    # Public API - auto-managed session

    async def create(self, data: SessionCreate) -> SessionRead:
        """Create a new session.

        Args:
            data: Session creation data

        Returns:
            Created session
        """
        return self._create_sqlite(data)

    async def get(self, session_id: str) -> SessionRead | None:
        """Get session by ID.

        Args:
            session_id: Session ID

        Returns:
            Session or None if not found
        """
        return self._get_sqlite(session_id)

    async def update(self, session_id: str, data: SessionUpdate) -> SessionRead | None:
        """Update session.

        Args:
            session_id: Session ID
            data: Update data

        Returns:
            Updated session or None if not found
        """
        return self._update_sqlite(session_id, data)

    async def delete(self, session_id: str) -> bool:
        """Delete session.

        Args:
            session_id: Session ID

        Returns:
            True if deleted, False if not found
        """
        return self._delete_sqlite(session_id)

    async def list_recent(self, limit: int = 20) -> list[SessionRead]:
        """Get recent sessions.

        Args:
            limit: Maximum number of sessions to return

        Returns:
            List of sessions ordered by updated_at desc
        """
        return self._list_recent_sqlite(limit)

    async def list_by_status(self, status: str, limit: int = 100) -> list[SessionRead]:
        """Get sessions by status.

        Args:
            status: Session status
            limit: Maximum number of sessions to return

        Returns:
            List of sessions
        """
        return self._list_by_status_sqlite(status, limit)

    @staticmethod
    def _row_to_read(row) -> SessionRead:
        return SessionRead(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=row["status"],
            meta_info=loads_json(row["meta_info"], {}),
            conversation_count=row["conversation_count"],
            total_tokens=row["total_tokens"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _create_sqlite(self, data: SessionCreate) -> SessionRead:
        now = get_east_8_time().isoformat()
        session_id = new_id()
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, title, description, status, meta_info,
                    conversation_count, total_tokens, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    session_id,
                    data.title,
                    data.description,
                    SessionStatus.ACTIVE.value,
                    dumps_json(data.meta_info),
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._row_to_read(row)

    def _get_sqlite(self, session_id: str) -> SessionRead | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._row_to_read(row) if row else None

    def _update_sqlite(
        self, session_id: str, data: SessionUpdate
    ) -> SessionRead | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None

            meta_info = (
                dumps_json(data.meta_info)
                if data.meta_info is not None
                else row["meta_info"]
            )
            updated_at = get_east_8_time().isoformat()
            conn.execute(
                """
                UPDATE sessions
                SET title = ?, description = ?, status = ?, meta_info = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    data.title if data.title is not None else row["title"],
                    data.description
                    if data.description is not None
                    else row["description"],
                    data.status if data.status is not None else row["status"],
                    meta_info,
                    updated_at,
                    session_id,
                ),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._row_to_read(updated)

    def _delete_sqlite(self, session_id: str) -> bool:
        with connect() as conn:
            result = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
        return result.rowcount > 0

    def _list_recent_sqlite(self, limit: int) -> list[SessionRead]:
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_read(row) for row in rows]

    def _list_by_status_sqlite(self, status: str, limit: int) -> list[SessionRead]:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE status = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        return [self._row_to_read(row) for row in rows]

    # Private implementation - requires manual session

    async def _create(self, db: AsyncSession, data: SessionCreate) -> SessionRead:
        """Create session implementation."""
        obj = Session(
            title=data.title,
            description=data.description,
            meta_info=data.meta_info,
            status=SessionStatus.ACTIVE.value,
            created_at=get_east_8_time(),
            updated_at=get_east_8_time(),
        )
        db.add(obj)
        await db.commit()
        await db.refresh(obj)
        return SessionRead.model_validate(obj)

    async def _get(self, db: AsyncSession, session_id: str) -> SessionRead | None:
        """Get session implementation."""
        obj = await db.get(Session, session_id)
        if obj is None:
            return None
        return SessionRead.model_validate(obj)

    async def _update(
        self, db: AsyncSession, session_id: str, data: SessionUpdate
    ) -> SessionRead | None:
        """Update session implementation."""
        obj = await db.get(Session, session_id)
        if obj is None:
            return None

        if data.title is not None:
            obj.title = data.title
        if data.description is not None:
            obj.description = data.description
        if data.status is not None:
            obj.status = data.status
        if data.meta_info is not None:
            obj.meta_info = data.meta_info

        # Update timestamp to East 8 time
        obj.updated_at = get_east_8_time()

        db.add(obj)
        await db.commit()
        await db.refresh(obj)
        return SessionRead.model_validate(obj)

    async def _delete(self, db: AsyncSession, session_id: str) -> bool:
        """Delete session implementation."""
        obj = await db.get(Session, session_id)
        if obj is None:
            return False
        await db.delete(obj)
        await db.commit()
        return True

    async def _list_recent(self, db: AsyncSession, limit: int) -> list[SessionRead]:
        """List recent sessions implementation."""
        stmt = select(Session).order_by(desc(Session.updated_at)).limit(limit)
        result = await db.execute(stmt)
        rows = result.scalars().all()
        return [SessionRead.model_validate(x) for x in rows]

    async def _list_by_status(
        self, db: AsyncSession, status: str, limit: int
    ) -> list[SessionRead]:
        """List by status implementation."""
        stmt = (
            select(Session)
            .where(Session.status == status)
            .order_by(desc(Session.updated_at))
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        return [SessionRead.model_validate(x) for x in rows]
