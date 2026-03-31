"""CLI commands for Memento-S."""

from .agent import agent_command
from .candidates import (
    candidates_diff_command,
    candidates_list_command,
    candidates_promote_command,
    candidates_reject_command,
    candidates_show_command,
)
from .doctor import doctor_command
from .feishu_bridge import feishu_bridge_command
from .verify import verify_command

__all__ = [
    "agent_command",
    "candidates_diff_command",
    "candidates_list_command",
    "candidates_promote_command",
    "candidates_reject_command",
    "candidates_show_command",
    "doctor_command",
    "feishu_bridge_command",
    "verify_command",
]
