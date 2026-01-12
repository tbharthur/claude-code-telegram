# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Telegram bot that provides remote access to Claude Code, enabling developers to interact with their projects via Telegram. Uses python-telegram-bot for Telegram integration and the Anthropic SDK (or Claude CLI as fallback) for Claude Code access.

## Common Commands

```bash
# Install dependencies
make dev

# Run in debug mode (console logging)
make run-debug

# Run in production mode (JSON logging)
make run

# Run tests with coverage
make test

# Run a single test file
poetry run pytest tests/unit/test_config.py -v

# Run a specific test
poetry run pytest tests/unit/test_config.py::test_function_name -v

# Lint (black, isort, flake8, mypy)
make lint

# Auto-format code
make format
```

## Architecture

### Entry Point
`src/main.py` - Creates and wires all components via dependency injection. The `create_application()` function builds the component graph.

### Core Modules

**`src/bot/`** - Telegram bot layer
- `core.py` - `ClaudeCodeBot` orchestrator, handler registration
- `handlers/command.py` - Telegram command handlers (/start, /help, /continue, /status, /stop)
- `handlers/message.py` - Free-text message handling, forwards to Claude
- `handlers/callback.py` - Inline keyboard callback handlers
- `features/` - Feature modules (file uploads, image handling, conversation mode)

**`src/claude/`** - Claude Code integration
- `facade.py` - `ClaudeIntegration` main entry point, coordinates SDK/CLI with session and tool monitoring
- `sdk_integration.py` - Python SDK integration via `claude-code-sdk`
- `integration.py` - CLI subprocess fallback (`ClaudeProcessManager`)
- `session.py` - `SessionManager` for conversation state
- `persistent.py` - Keeps Claude process alive across messages
- `parser.py` - Parses Claude CLI output
- `monitor.py` - `ToolMonitor` validates tool calls against security rules

**`src/security/`** - Authentication and security
- `auth.py` - `AuthenticationManager` with whitelist and token providers
- `validators.py` - `SecurityValidator` for path validation, sandbox enforcement
- `rate_limiter.py` - Token bucket rate limiting
- `audit.py` - Security event logging

**`src/storage/`** - Persistence layer
- `facade.py` - `Storage` facade over database
- `database.py` - SQLite async database manager with migrations
- `repositories.py` - Data access patterns (users, sessions, usage)
- `session_storage.py` - Claude session persistence

**`src/config/`** - Configuration
- `settings.py` - Pydantic settings model
- `loader.py` - Loads from .env with validation
- `features.py` - Feature flags

### Key Patterns

- **Facade pattern**: `ClaudeIntegration` and `Storage` provide simplified interfaces over complex subsystems
- **Dependency injection**: Components receive dependencies via constructor, wired in `main.py`
- **Async throughout**: All I/O operations are async using asyncio
- **Structured logging**: Uses structlog with JSON output in production

### Configuration

Settings loaded from `.env` file. Key settings:
- `USE_SDK=true` - Use Python SDK (recommended) vs CLI subprocess
- `APPROVED_DIRECTORY` - Sandbox root for file access
- `ALLOWED_USERS` - Telegram user ID whitelist
- `CLAUDE_ALLOWED_TOOLS` - Permitted Claude tools

## Code Standards

- Python 3.10+ with type hints on all functions
- Black formatting (88 char lines)
- Tests use pytest-asyncio with `asyncio_mode = "auto"`
