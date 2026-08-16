"""Tool registry.

Every module in this package exposes two things:

    TOOLS     - a list of Anthropic tool schemas
    HANDLERS  - a dict mapping tool name -> callable(tool_input) -> str

This module aggregates them so `brain.py` never has to know which module owns
which tool.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `import config` work no matter how TBS was started (from core/, from
# the project root, or as a module).
_CORE_DIR = str(Path(__file__).resolve().parent.parent)
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

from . import apps, files, memory, projects, schedule, system_info, web  # noqa: E402

MODULES = (system_info, apps, files, web, schedule, memory, projects)

TOOL_SCHEMAS: list[dict] = []
_HANDLERS: dict[str, object] = {}

for _module in MODULES:
    for _schema in _module.TOOLS:
        name = _schema["name"]
        if name in _HANDLERS:
            raise RuntimeError(f"Duplicate tool name '{name}' in {_module.__name__}")
        TOOL_SCHEMAS.append(_schema)
    _HANDLERS.update(_module.HANDLERS)

TOOL_NAMES = tuple(sorted(_HANDLERS))


def dispatch(name: str, tool_input: dict | None) -> tuple[str, bool]:
    """Run a tool. Returns (result_text, is_error).

    Never raises: a failing tool comes back as an error string so the model can
    read it and adjust instead of the whole assistant crashing.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return (f"Unknown tool '{name}'. Available: {', '.join(TOOL_NAMES)}.", True)
    try:
        result = handler(tool_input or {})  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 - tool failures must not kill the loop
        return (f"{name} failed: {type(exc).__name__}: {exc}", True)
    text = "" if result is None else str(result)
    return (text or "(the tool returned nothing)", False)
