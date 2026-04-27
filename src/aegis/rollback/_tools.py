"""Canonical tool-name sets used by rollback strategies.

Donor: aegis-mvp v1.0.0 ``atmu/rules/_tools.py``. Imported here as a
private module of ``aegis.rollback`` because the rollback strategies
need to dispatch on tool name. When D11 ports the broader ATMU rule
classes, this can be promoted into a shared module under ``aegis/``
proper.
"""

from __future__ import annotations

# Shell-class tools that can run arbitrary commands.
SHELL_TOOLS: frozenset[str] = frozenset({
    # Claude Code canonical
    "Bash",
    # Legacy / aliases / MCP variants
    "shell", "bash", "exec", "sh", "zsh", "fish",
    "execute_shell", "run_command", "terminal",
})

# File-mutating tools.
FILE_WRITE_TOOLS: frozenset[str] = frozenset({
    "Write", "Edit", "MultiEdit",
    "write_file", "edit_file", "create_file", "modify_file",
    "fopen", "open", "FileWrite", "SaveFile",
})

# File-reading tools (path-traversal applies).
FILE_READ_TOOLS: frozenset[str] = frozenset({
    "Read", "Grep", "Glob",
    "read_file", "search_files", "list_files",
    "FileRead", "OpenFile",
})

FILE_TOOLS: frozenset[str] = FILE_WRITE_TOOLS | FILE_READ_TOOLS
