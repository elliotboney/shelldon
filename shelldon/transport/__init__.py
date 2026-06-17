"""transport — pluggable chat-transport adapters (CLI first; Telegram etc. later)."""

from shelldon.transport.cli import run_cli_transport

__all__ = ["run_cli_transport"]
