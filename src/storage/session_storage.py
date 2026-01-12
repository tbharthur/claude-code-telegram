"""Persistent session storage implementation.

Replaces the in-memory session storage with SQLite persistence.
"""

from datetime import datetime
from pathlib import Path
from typing import List, Optional

import structlog

from ..claude.session import ClaudeSession, SessionStorage
from .database import DatabaseManager
from .models import SessionModel, UserModel

logger = structlog.get_logger()


class SQLiteSessionStorage(SessionStorage):
    """SQLite-based session storage."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize with database manager."""
        self.db_manager = db_manager

    async def _ensure_user_exists(
        self, user_id: int, username: Optional[str] = None
    ) -> None:
        """Ensure user exists in database before creating session."""
        async with self.db_manager.get_connection() as conn:
            # Check if user exists
            cursor = await conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
            )
            user_exists = await cursor.fetchone()

            if not user_exists:
                # Create user record
                now = datetime.utcnow()
                await conn.execute(
                    """
                    INSERT INTO users (user_id, telegram_username, first_seen, last_active, is_allowed)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        now,
                        now,
                        True,
                    ),  # Allow user by default for now
                )
                await conn.commit()

                logger.info(
                    "Created user record for session",
                    user_id=user_id,
                    username=username,
                )

    async def save_session(self, session: ClaudeSession) -> None:
        """Save session to database."""
        # Ensure user exists before creating session
        await self._ensure_user_exists(session.user_id)

        session_model = SessionModel(
            session_id=session.session_id,
            user_id=session.user_id,
            project_path=str(session.project_path),
            created_at=session.created_at,
            last_used=session.last_used,
            total_cost=session.total_cost,
            total_turns=session.total_turns,
            message_count=session.message_count,
            thread_id=session.thread_id,
        )

        async with self.db_manager.get_connection() as conn:
            # Use INSERT ... ON CONFLICT to handle race conditions atomically
            await conn.execute(
                """
                INSERT INTO sessions
                (session_id, user_id, project_path, created_at, last_used,
                 total_cost, total_turns, message_count, thread_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    last_used = excluded.last_used,
                    total_cost = excluded.total_cost,
                    total_turns = excluded.total_turns,
                    message_count = excluded.message_count,
                    thread_id = excluded.thread_id
                """,
                (
                    session_model.session_id,
                    session_model.user_id,
                    session_model.project_path,
                    session_model.created_at,
                    session_model.last_used,
                    session_model.total_cost,
                    session_model.total_turns,
                    session_model.message_count,
                    session_model.thread_id,
                ),
            )

            await conn.commit()

        logger.debug(
            "Session saved to database",
            session_id=session.session_id,
            user_id=session.user_id,
            thread_id=session.thread_id,
        )

    async def load_session(self, session_id: str) -> Optional[ClaudeSession]:
        """Load session from database."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            )
            row = await cursor.fetchone()

            if not row:
                return None

            session_model = SessionModel.from_row(row)

            # Convert to ClaudeSession
            claude_session = ClaudeSession(
                session_id=session_model.session_id,
                user_id=session_model.user_id,
                project_path=Path(session_model.project_path),
                created_at=session_model.created_at,
                last_used=session_model.last_used,
                total_cost=session_model.total_cost,
                total_turns=session_model.total_turns,
                message_count=session_model.message_count,
                tools_used=[],  # Tools are tracked separately in tool_usage table
                thread_id=session_model.thread_id,
            )

            logger.debug(
                "Session loaded from database",
                session_id=session_id,
                user_id=claude_session.user_id,
                thread_id=claude_session.thread_id,
            )

            return claude_session

    async def delete_session(self, session_id: str) -> None:
        """Delete session from database."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                "UPDATE sessions SET is_active = FALSE WHERE session_id = ?",
                (session_id,),
            )
            await conn.commit()

        logger.debug("Session marked as inactive", session_id=session_id)

    async def get_user_sessions(self, user_id: int) -> List[ClaudeSession]:
        """Get all active sessions for a user."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM sessions
                WHERE user_id = ? AND is_active = TRUE
                ORDER BY last_used DESC
            """,
                (user_id,),
            )
            rows = await cursor.fetchall()

            sessions = []
            for row in rows:
                session_model = SessionModel.from_row(row)
                claude_session = ClaudeSession(
                    session_id=session_model.session_id,
                    user_id=session_model.user_id,
                    project_path=Path(session_model.project_path),
                    created_at=session_model.created_at,
                    last_used=session_model.last_used,
                    total_cost=session_model.total_cost,
                    total_turns=session_model.total_turns,
                    message_count=session_model.message_count,
                    tools_used=[],  # Tools are tracked separately
                    thread_id=session_model.thread_id,
                )
                sessions.append(claude_session)

            return sessions

    async def get_all_sessions(self) -> List[ClaudeSession]:
        """Get all active sessions."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE is_active = TRUE ORDER BY last_used DESC"
            )
            rows = await cursor.fetchall()

            sessions = []
            for row in rows:
                session_model = SessionModel.from_row(row)
                claude_session = ClaudeSession(
                    session_id=session_model.session_id,
                    user_id=session_model.user_id,
                    project_path=Path(session_model.project_path),
                    created_at=session_model.created_at,
                    last_used=session_model.last_used,
                    total_cost=session_model.total_cost,
                    total_turns=session_model.total_turns,
                    message_count=session_model.message_count,
                    tools_used=[],  # Tools are tracked separately
                    thread_id=session_model.thread_id,
                )
                sessions.append(claude_session)

            return sessions

    async def cleanup_expired_sessions(self, timeout_hours: int) -> int:
        """Mark expired sessions as inactive."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE sessions
                SET is_active = FALSE
                WHERE last_used < datetime('now', '-' || ? || ' hours')
                  AND is_active = TRUE
            """,
                (timeout_hours,),
            )
            await conn.commit()

            affected = cursor.rowcount
            logger.info(
                "Cleaned up expired sessions",
                count=affected,
                timeout_hours=timeout_hours,
            )
            return affected

    async def set_user_active_session(
        self,
        user_id: int,
        thread_id: Optional[int],
        session_id: str,
        project_path: str,
    ) -> None:
        """Store the user's active session for resume after restart."""
        # Use NULL for None thread_id (main chat) - SQLite handles NULL in UNIQUE constraints
        # with COALESCE to ensure consistent matching
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO user_active_sessions (user_id, thread_id, session_id, project_path, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(user_id, COALESCE(thread_id, 0)) DO UPDATE SET
                    session_id = excluded.session_id,
                    project_path = excluded.project_path,
                    updated_at = datetime('now')
                """,
                (user_id, thread_id, session_id, project_path),
            )
            await conn.commit()

        logger.debug(
            "Saved user active session",
            user_id=user_id,
            thread_id=thread_id,
            session_id=session_id,
        )

    async def get_user_active_session(
        self, user_id: int, thread_id: Optional[int]
    ) -> Optional[tuple]:
        """Get the user's active session for resume after restart.

        Returns: (session_id, project_path) or None if not found.
        """
        async with self.db_manager.get_connection() as conn:
            # Use COALESCE to match NULL thread_id consistently (NULL -> 0 for comparison)
            if thread_id is None:
                cursor = await conn.execute(
                    """
                    SELECT session_id, project_path FROM user_active_sessions
                    WHERE user_id = ? AND thread_id IS NULL
                    """,
                    (user_id,),
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT session_id, project_path FROM user_active_sessions
                    WHERE user_id = ? AND thread_id = ?
                    """,
                    (user_id, thread_id),
                )
            row = await cursor.fetchone()

            if row:
                logger.debug(
                    "Found user active session",
                    user_id=user_id,
                    thread_id=thread_id,
                    session_id=row[0],
                )
                return (row[0], row[1])
            return None

    async def clear_user_active_session(
        self, user_id: int, thread_id: Optional[int]
    ) -> None:
        """Clear the user's active session (e.g., on /new command)."""
        async with self.db_manager.get_connection() as conn:
            # Use proper NULL handling for thread_id
            if thread_id is None:
                await conn.execute(
                    """
                    DELETE FROM user_active_sessions
                    WHERE user_id = ? AND thread_id IS NULL
                    """,
                    (user_id,),
                )
            else:
                await conn.execute(
                    """
                    DELETE FROM user_active_sessions
                    WHERE user_id = ? AND thread_id = ?
                    """,
                    (user_id, thread_id),
                )
            await conn.commit()

        logger.debug(
            "Cleared user active session",
            user_id=user_id,
            thread_id=thread_id,
        )
