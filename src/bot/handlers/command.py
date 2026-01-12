"""Command handlers for bot operations."""

from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ...security.audit import AuditLogger

logger = structlog.get_logger()

__all__ = [
    "start_command",
    "help_command",
    "continue_session",
    "session_status",
    "stop_command",
]


def _get_thread_id(update: Update) -> Optional[int]:
    """Get message_thread_id for threaded mode support."""
    if update.message and update.message.message_thread_id:
        return update.message.message_thread_id
    return None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = update.effective_user

    welcome_message = (
        f"Welcome to Claude Code, {user.first_name}!\n\n"
        f"Send any message to start coding.\n"
        f"Use /clear to start fresh, /continue to resume, /status to check session."
    )

    await update.message.reply_text(welcome_message, parse_mode="Markdown")

    # Log command
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    if audit_logger:
        await audit_logger.log_command(
            user_id=user.id, command="start", args=[], success=True
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    help_text = (
        "**Claude Code Telegram Bot Help**\n\n"
        "**Commands:**\n"
        "• `/continue [message]` - Continue last session (optionally with message)\n"
        "• `/status` - Show session and usage status\n"
        "• `/stop` - Stop current operation\n\n"
        "**Usage:**\n"
        "• Send any message to interact with Claude\n"
        "• Send a file for Claude to review it\n"
        "• Use Claude slash commands like `/commit`, `/review`\n"
        "• Use /clear to start a fresh session\n\n"
        "**File Operations:**\n"
        "• Send text files (.py, .js, .md, etc.) for review\n"
        "• Claude can read, modify, and create files\n"
        "• All file operations are within your approved directory\n\n"
        "**Tips:**\n"
        "• Use specific, clear requests for best results\n"
        "• Check `/status` to monitor your usage\n"
        "• File uploads are automatically processed by Claude"
    )

    await update.message.reply_text(help_text, parse_mode="Markdown")


async def continue_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /continue command with optional prompt."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    thread_id = _get_thread_id(update)

    # Parse optional prompt from command arguments
    prompt = " ".join(context.args) if context.args else None

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        if not claude_integration:
            await update.message.reply_text(
                "❌ **Claude Integration Not Available**\n\n"
                "Claude integration is not properly configured."
            )
            return

        # Check if there's an existing session in user context
        claude_session_id = context.user_data.get("claude_session_id")
        restored_from_db = False

        # If no session in context, try to load from persistent storage (survives restarts)
        if not claude_session_id:
            try:
                active_session = await claude_integration.get_user_active_session(
                    user_id, thread_id
                )
                if active_session:
                    claude_session_id = active_session[0]
                    restored_from_db = True
                    # Immediately save to context to prevent loss if exception occurs later
                    context.user_data["claude_session_id"] = claude_session_id
                    # Also restore the directory if it was stored
                    stored_path = active_session[1]
                    if stored_path:
                        from pathlib import Path
                        stored_dir = Path(stored_path)
                        # Validate restored directory exists and is within approved path
                        if stored_dir.exists() and str(stored_dir.resolve()).startswith(str(settings.approved_directory.resolve())):
                            current_dir = stored_dir
                            context.user_data["current_directory"] = current_dir
                        else:
                            logger.warning(
                                "Restored directory invalid or outside approved path",
                                stored_path=str(stored_dir),
                                approved_directory=str(settings.approved_directory),
                            )
                    logger.info(
                        "Restored session from persistent storage",
                        session_id=claude_session_id,
                        user_id=user_id,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to restore session from persistent storage",
                    error=str(e),
                    user_id=user_id,
                )
                active_session = None

        if claude_session_id:
            # Build status message - note if session was restored after restart
            restore_note = ""
            if restored_from_db:
                restore_note = "\n_(Session restored after bot restart)_\n"

            status_msg = await update.message.reply_text(
                f"🔄 **Continuing Session**\n\n"
                f"Session ID: `{claude_session_id[:8]}...`\n"
                f"Directory: `{current_dir.relative_to(settings.approved_directory)}/`\n"
                f"{restore_note}\n"
                f"{'Processing your message...' if prompt else 'Continuing where you left off...'}",
                parse_mode="Markdown",
            )

            # Continue with the existing session
            claude_response = await claude_integration.run_command(
                prompt=prompt or "",
                working_directory=current_dir,
                user_id=user_id,
                session_id=claude_session_id,
                thread_id=thread_id,
            )
        else:
            # No session in context, try to find the most recent session
            status_msg = await update.message.reply_text(
                "🔍 **Looking for Recent Session**\n\n"
                "Searching for your most recent session in this directory...",
                parse_mode="Markdown",
            )

            claude_response = await claude_integration.continue_session(
                user_id=user_id,
                working_directory=current_dir,
                prompt=prompt,
                thread_id=thread_id,
            )

        if claude_response:
            # Update session ID in context
            context.user_data["claude_session_id"] = claude_response.session_id

            # Persist session for resume after restart
            await claude_integration.set_user_active_session(
                user_id, thread_id, claude_response.session_id, current_dir
            )

            # Delete status message and send response
            await status_msg.delete()

            # Format and send Claude's response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(claude_response.content)

            for msg in formatted_messages:
                await update.message.reply_text(
                    msg.text,
                    parse_mode="Markdown",
                    reply_markup=msg.reply_markup,
                )

            # Log successful continue
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="continue",
                    args=context.args or [],
                    success=True,
                )

        else:
            # No session found to continue
            await status_msg.edit_text(
                "❌ **No Session Found**\n\n"
                f"No recent Claude session found in this directory.\n"
                f"Directory: `{current_dir.relative_to(settings.approved_directory)}/`\n\n"
                f"Send any message to start a new session.",
                parse_mode="Markdown",
            )

    except Exception as e:
        error_msg = str(e)
        logger.error("Error in continue command", error=error_msg, user_id=user_id)

        # Delete status message if it exists
        try:
            if "status_msg" in locals():
                await status_msg.delete()
        except Exception:
            pass

        # Send error response
        await update.message.reply_text(
            f"❌ **Error Continuing Session**\n\n"
            f"An error occurred while trying to continue your session:\n\n"
            f"`{error_msg}`\n\n"
            f"Send any message to start a new session, or use `/status` to check status.",
            parse_mode="Markdown",
        )

        # Log failed continue
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="continue",
                args=context.args or [],
                success=False,
            )


async def session_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    user_id = update.effective_user.id
    thread_id = _get_thread_id(update)
    settings: Settings = context.bot_data["settings"]
    claude_integration = context.bot_data.get("claude_integration")

    # Get session info
    claude_session_id = context.user_data.get("claude_session_id")
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Get rate limiter info if available
    rate_limiter = context.bot_data.get("rate_limiter")
    usage_info = ""
    if rate_limiter:
        try:
            user_status = rate_limiter.get_user_status(user_id)
            cost_usage = user_status.get("cost_usage", {})
            current_cost = cost_usage.get("current", 0.0)
            cost_limit = cost_usage.get("limit", settings.claude_max_cost_per_user)
            cost_percentage = (current_cost / cost_limit) * 100 if cost_limit > 0 else 0

            usage_info = f"💰 Usage: ${current_cost:.2f} / ${cost_limit:.2f} ({cost_percentage:.0f}%)\n"
        except Exception:
            usage_info = "💰 Usage: _Unable to retrieve_\n"

    # Get context window status from persistent manager
    context_info = ""
    sessions_info = ""
    if claude_integration and hasattr(claude_integration, "persistent_manager"):
        try:
            current_session_status = claude_integration.persistent_manager.get_session_status(user_id, thread_id)
            if current_session_status:
                tokens_used = current_session_status.get("context_tokens_used", 0)
                tokens_max = current_session_status.get("context_tokens_max", 200000)
                context_pct = current_session_status.get("context_percentage", 0)
                msg_count = current_session_status.get("message_count", 0)

                # Format tokens in K
                tokens_used_k = tokens_used / 1000
                tokens_max_k = tokens_max / 1000

                # Context bar visualization
                bar_length = 10
                filled = int(context_pct / 100 * bar_length)
                bar = "█" * filled + "░" * (bar_length - filled)

                context_info = f"📝 Context: [{bar}] {tokens_used_k:.0f}K / {tokens_max_k:.0f}K ({context_pct:.0f}%)\n"
                context_info += f"💬 Messages: {msg_count}\n"
        except Exception:
            pass

        # Get all active sessions
        try:
            all_sessions = claude_integration.persistent_manager.get_all_sessions_info()
            session_count = len(all_sessions)
            if session_count > 0:
                sessions_info = f"🧵 Active Sessions: {session_count}\n"
                for i, sess in enumerate(all_sessions, 1):
                    sess_thread_id = sess.get("thread_id")
                    sess_user_id = sess.get("user_id")
                    sess_msgs = sess.get("message_count", 0)
                    sess_ctx = sess.get("context_percentage", 0)
                    # Mark current session
                    is_current = (sess_user_id == user_id and sess_thread_id == thread_id)
                    marker = " 👈" if is_current else ""
                    # Format thread identifier
                    if sess_thread_id:
                        thread_label = f"Topic #{sess_thread_id}"
                    else:
                        thread_label = "Main chat"
                    sessions_info += f"  {i}. {thread_label} ({sess_msgs} msgs, {sess_ctx:.0f}%){marker}\n"
        except Exception:
            pass

    # Format status message
    status_lines = [
        "📊 **Session Status**",
        "",
        f"📂 Directory: `{relative_path}/`",
        f"🤖 Claude Session: {'✅ Active' if claude_session_id else '❌ None'}",
    ]

    if context_info:
        status_lines.append(context_info.rstrip())

    if sessions_info:
        status_lines.append(sessions_info.rstrip())

    status_lines.extend([
        usage_info.rstrip() if usage_info else "",
        f"🕐 Last Update: {update.message.date.strftime('%H:%M:%S UTC')}",
    ])

    # Filter out empty lines
    status_lines = [line for line in status_lines if line]

    if claude_session_id:
        status_lines.append(f"🆔 Session ID: `{claude_session_id[:8]}...`")

    # Add Refresh button only
    keyboard = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_status")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "\n".join(status_lines), parse_mode="Markdown", reply_markup=reply_markup
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop command to interrupt Claude's current operation."""
    user_id = update.effective_user.id
    thread_id = _get_thread_id(update)

    # Get the persistent manager from claude integration
    claude: ClaudeIntegration = context.bot_data.get("claude")
    if not claude or not hasattr(claude, "persistent_manager"):
        await update.message.reply_text(
            "❌ **Cannot Interrupt**\n\n"
            "No active Claude process to interrupt."
        )
        return

    # Try to interrupt the session
    success = await claude.persistent_manager.interrupt_session(user_id, thread_id)

    if success:
        await update.message.reply_text(
            "🛑 **Interrupt Sent**\n\n"
            "Sent interrupt signal to Claude. It should stop its current operation.\n\n"
            "If Claude doesn't respond, use /clear to start fresh."
        )
        logger.info("Interrupt signal sent", user_id=user_id, thread_id=thread_id)
    else:
        await update.message.reply_text(
            "ℹ️ **No Active Process**\n\n"
            "No active Claude process to interrupt.\n\n"
            "Send any message to start coding."
        )
