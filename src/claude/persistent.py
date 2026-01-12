"""Persistent Claude Code process manager.

Keeps a single Claude process running per user session, sending messages
via stdin instead of spawning new processes.
"""

import asyncio
import json
from asyncio.subprocess import Process
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import structlog

from ..config.settings import Settings
from .integration import ClaudeResponse, StreamUpdate

logger = structlog.get_logger()


@dataclass
class PersistentSession:
    """A persistent Claude session with a running process."""
    process: Process
    session_id: Optional[str]
    working_directory: Path
    user_id: int
    thread_id: Optional[int]
    lock: asyncio.Lock
    # Context tracking (updated after each response)
    context_tokens_used: int = 0
    context_tokens_max: int = 0
    total_cost: float = 0.0
    message_count: int = 0


class PersistentClaudeManager:
    """Manages persistent Claude processes per user/thread."""

    def __init__(self, config: Settings):
        self.config = config
        # Key: (user_id, thread_id) where thread_id can be None for non-threaded chats
        self.sessions: Dict[tuple, PersistentSession] = {}
        self._cleanup_lock = asyncio.Lock()

    def _session_key(self, user_id: int, thread_id: Optional[int] = None) -> tuple:
        """Create session key from user_id and thread_id."""
        return (user_id, thread_id)

    async def get_or_create_session(
        self,
        user_id: int,
        working_directory: Path,
        session_id: Optional[str] = None,
        thread_id: Optional[int] = None,
    ) -> PersistentSession:
        """Get existing session or create new one."""
        key = self._session_key(user_id, thread_id)

        # Check for existing session
        if key in self.sessions:
            session = self.sessions[key]
            # Check if process is still alive
            if session.process.returncode is None:
                # Kill session if working directory or session_id changed
                if session.working_directory != working_directory:
                    logger.info(
                        "Killing session due to directory change",
                        user_id=user_id,
                        thread_id=thread_id,
                        old_dir=str(session.working_directory),
                        new_dir=str(working_directory),
                    )
                    await self.kill_session(user_id, thread_id)
                elif session_id and session.session_id != session_id:
                    # Session ID changed - need to restart with --resume for the new session
                    logger.info(
                        "Killing session to resume different session",
                        user_id=user_id,
                        thread_id=thread_id,
                        old_session=session.session_id,
                        new_session=session_id,
                    )
                    await self.kill_session(user_id, thread_id)
                else:
                    return session
            else:
                # Process died, clean up
                del self.sessions[key]

        # Create new persistent process
        process = await self._start_persistent_process(working_directory, session_id)

        session = PersistentSession(
            process=process,
            session_id=session_id,
            working_directory=working_directory,
            user_id=user_id,
            thread_id=thread_id,
            lock=asyncio.Lock(),
        )
        self.sessions[key] = session

        logger.info(
            "Created persistent Claude session",
            user_id=user_id,
            thread_id=thread_id,
            working_directory=str(working_directory),
        )

        return session

    async def _start_persistent_process(
        self, working_directory: Path, session_id: Optional[str] = None
    ) -> Process:
        """Start a persistent Claude process with streaming I/O."""
        cmd = [self.config.claude_binary_path or "claude"]

        # Use streaming JSON for both input and output
        cmd.extend(["--input-format", "stream-json"])
        cmd.extend(["--output-format", "stream-json"])
        cmd.extend(["--verbose"])
        cmd.extend(["--dangerously-skip-permissions"])
        cmd.extend(["--max-turns", str(self.config.claude_max_turns)])

        # Resume session if provided
        if session_id:
            cmd.extend(["--resume", session_id])

        # Add allowed tools
        if hasattr(self.config, "claude_allowed_tools") and self.config.claude_allowed_tools:
            cmd.extend(["--allowedTools", ",".join(self.config.claude_allowed_tools)])

        logger.debug("Starting persistent Claude process", command=cmd)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(working_directory),
            # Increase buffer limit to handle large JSON outputs (10MB)
            limit=10 * 1024 * 1024,
        )

        return process

    async def send_message(
        self,
        user_id: int,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        thread_id: Optional[int] = None,
    ) -> ClaudeResponse:
        """Send a message to the persistent Claude process."""

        session = await self.get_or_create_session(user_id, working_directory, session_id, thread_id)

        async with session.lock:
            return await self._send_and_receive(session, prompt, stream_callback)

    async def _send_and_receive(
        self,
        session: PersistentSession,
        prompt: str,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> ClaudeResponse:
        """Send message and receive response."""

        # Build the input message
        input_msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": prompt
            }
        }

        # Send to stdin
        input_line = json.dumps(input_msg) + "\n"
        session.process.stdin.write(input_line.encode())
        await session.process.stdin.drain()

        logger.debug("Sent message to Claude", prompt_length=len(prompt))

        # Read response
        return await self._read_response(session, stream_callback)

    async def _read_response(
        self,
        session: PersistentSession,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> ClaudeResponse:
        """Read response from Claude until result message."""

        messages = []
        result = None

        try:
            while True:
                # Read with timeout
                line = await asyncio.wait_for(
                    session.process.stdout.readline(),
                    timeout=self.config.claude_timeout_seconds,
                )

                if not line:
                    # Process ended
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    msg = json.loads(line_str)
                    messages.append(msg)

                    # Update session_id if we get one
                    if msg.get("session_id") and not session.session_id:
                        session.session_id = msg.get("session_id")

                    # Stream callback
                    if stream_callback:
                        update = self._parse_stream_message(msg)
                        if update:
                            try:
                                await stream_callback(update)
                            except Exception as e:
                                logger.warning("Stream callback failed", error=str(e))

                    # Check for result (end of response)
                    if msg.get("type") == "result":
                        result = msg
                        break

                except json.JSONDecodeError:
                    logger.warning("Failed to parse JSON", line=line_str[:100])
                    continue

        except asyncio.TimeoutError:
            logger.error("Timeout waiting for Claude response")
            # Kill and remove the session
            await self.kill_session(session.user_id, session.thread_id)
            raise

        if not result:
            raise Exception("No result received from Claude")

        # Extract context window info from nested structure
        context_window = result.get("context_window", {})
        current_usage = context_window.get("current_usage") or {}

        # Current context usage = input + cache tokens
        context_tokens_used = (
            current_usage.get("input_tokens", 0) +
            current_usage.get("cache_creation_input_tokens", 0) +
            current_usage.get("cache_read_input_tokens", 0)
        )
        context_tokens_max = context_window.get("context_window_size", 200000)
        cost = result.get("cost_usd", 0.0)

        # Update session tracking
        session.context_tokens_used = context_tokens_used
        session.context_tokens_max = context_tokens_max
        session.total_cost += cost
        session.message_count += 1

        return ClaudeResponse(
            content=result.get("result", ""),
            session_id=result.get("session_id", session.session_id or ""),
            cost=cost,
            duration_ms=result.get("duration_ms", 0),
            num_turns=result.get("num_turns", 0),
            is_error=result.get("is_error", False),
            tools_used=[],
            context_tokens_used=context_tokens_used,
            context_tokens_max=context_tokens_max,
        )

    def _parse_stream_message(self, msg: Dict[str, Any]) -> Optional[StreamUpdate]:
        """Parse stream message into StreamUpdate."""
        msg_type = msg.get("type")

        if msg_type == "assistant":
            message = msg.get("message", {})
            content_blocks = message.get("content", [])
            text_content = []
            for block in content_blocks:
                if block.get("type") == "text":
                    text_content.append(block.get("text", ""))
            return StreamUpdate(
                type="assistant",
                content="\n".join(text_content) if text_content else None,
            )
        elif msg_type == "system":
            return StreamUpdate(type="system", content=str(msg))

        return None

    async def interrupt_session(self, user_id: int, thread_id: Optional[int] = None) -> bool:
        """Send interrupt signal (SIGINT/ESC) to a user's session to stop current operation."""
        key = self._session_key(user_id, thread_id)
        if key not in self.sessions:
            return False

        session = self.sessions[key]
        if session.process.returncode is not None:
            # Process is dead
            return False

        try:
            import signal
            session.process.send_signal(signal.SIGINT)
            logger.info("Sent interrupt signal to session", user_id=user_id, thread_id=thread_id)
            return True
        except Exception as e:
            logger.warning("Error interrupting session", user_id=user_id, thread_id=thread_id, error=str(e))
            return False

    async def kill_session(self, user_id: int, thread_id: Optional[int] = None) -> None:
        """Kill a user's persistent session."""
        key = self._session_key(user_id, thread_id)
        if key in self.sessions:
            session = self.sessions[key]
            try:
                session.process.kill()
                await session.process.wait()
            except Exception as e:
                logger.warning("Error killing session", user_id=user_id, thread_id=thread_id, error=str(e))
            del self.sessions[key]
            logger.info("Killed persistent session", user_id=user_id, thread_id=thread_id)

    async def kill_all_sessions(self) -> None:
        """Kill all persistent sessions."""
        async with self._cleanup_lock:
            for (user_id, thread_id) in list(self.sessions.keys()):
                await self.kill_session(user_id, thread_id)

    def get_session_count(self) -> int:
        """Get number of active sessions."""
        return len(self.sessions)

    def get_session_status(self, user_id: int, thread_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Get context and usage status for a user's session."""
        key = self._session_key(user_id, thread_id)
        if key not in self.sessions:
            return None

        session = self.sessions[key]
        if session.process.returncode is not None:
            # Process is dead
            return None

        context_percentage = 0
        if session.context_tokens_max > 0:
            context_percentage = (session.context_tokens_used / session.context_tokens_max) * 100

        return {
            "session_id": session.session_id,
            "context_tokens_used": session.context_tokens_used,
            "context_tokens_max": session.context_tokens_max,
            "context_percentage": context_percentage,
            "total_cost": session.total_cost,
            "message_count": session.message_count,
            "working_directory": str(session.working_directory),
            "process_alive": session.process.returncode is None,
            "thread_id": session.thread_id,
        }

    def get_all_sessions_info(self) -> list[Dict[str, Any]]:
        """Get info about all active sessions."""
        sessions_info = []
        for (user_id, thread_id), session in self.sessions.items():
            if session.process.returncode is not None:
                # Process is dead, skip
                continue

            context_percentage = 0
            if session.context_tokens_max > 0:
                context_percentage = (session.context_tokens_used / session.context_tokens_max) * 100

            sessions_info.append({
                "user_id": user_id,
                "thread_id": thread_id,
                "session_id": session.session_id,
                "context_percentage": context_percentage,
                "message_count": session.message_count,
                "working_directory": str(session.working_directory),
            })

        return sessions_info
