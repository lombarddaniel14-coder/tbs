"""Configuration and settings for TBS.

Loads the Anthropic API key from the environment or a local `.env` file and
exposes the paths every other module uses. Fails loudly with a useful message
rather than silently misbehaving.
"""

from __future__ import annotations

import os
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when TBS cannot start because something is missing."""


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

CORE_DIR = Path(__file__).resolve().parent
ROOT_DIR = CORE_DIR.parent                      # ...\Fun Projects\TBS
PERSONALITY_DIR = ROOT_DIR / "personality"      # built by the voice/personality agent
DATA_DIR = ROOT_DIR / "data"                    # local JSON stores

SYSTEM_PROMPT_PATH = PERSONALITY_DIR / "system-prompt.md"
SCHEDULE_PATH = DATA_DIR / "schedule.json"
MEMORY_PATH = DATA_DIR / "memory.json"

# .env is searched for in the project root first, then in core/
ENV_CANDIDATES = (ROOT_DIR / ".env", CORE_DIR / ".env")


# --------------------------------------------------------------------------
# Model settings
# --------------------------------------------------------------------------

MODEL = "claude-opus-4-8"
MAX_TOKENS = 4096

# How many user turns of conversation to keep in the rolling window.
HISTORY_TURNS = 12

# Max tool-use round trips per user message (prevents runaway loops).
MAX_TOOL_ITERATIONS = 8

# Thinking: "off" keeps replies snappy for a voice assistant. Set
# TBS_THINKING=adaptive in .env if you want Claude to reason first.
THINKING_MODE = os.environ.get("TBS_THINKING", "off").strip().lower()

# Effort: low | medium | high | xhigh | max
EFFORT = os.environ.get("TBS_EFFORT", "low").strip().lower()

# Name TBS addresses the user by (used in the fallback system prompt).
USER_NAME = os.environ.get("TBS_USER_NAME", "the user")


# --------------------------------------------------------------------------
# Streaming and speech
# --------------------------------------------------------------------------

def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "stream")


# Speak each sentence as it is generated instead of waiting for the whole
# reply. TBS_STREAM=off restores the old behaviour.
STREAMING = _flag("TBS_STREAM", True)

# Print the wake -> transcript -> first token -> first audio -> done breakdown.
SHOW_TIMINGS = _flag("TBS_TIMINGS", True)

# Default text-to-speech backend. Piper is free, offline, and installed by
# `personality\setup_piper.py`; the personality layer reads the same variable.
TTS_BACKEND = os.environ.get("TBS_TTS", "piper").strip().lower()
os.environ.setdefault("TBS_TTS", TTS_BACKEND)


# --------------------------------------------------------------------------
# .env loading
# --------------------------------------------------------------------------

def load_dotenv() -> dict[str, str]:
    """Read the first `.env` found and push its keys into os.environ.

    Existing environment variables always win. Format is `KEY=value`, one per
    line; `#` starts a comment; quotes around the value are stripped.
    """
    loaded: dict[str, str] = {}
    for path in ENV_CANDIDATES:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - unreadable file
            raise ConfigError(f"Could not read {path}: {exc}") from exc
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            loaded[key] = value
            os.environ.setdefault(key, value)
        break  # first .env found wins
    return loaded


_API_KEY_HELP = """
TBS needs an Anthropic API key.

Do one of these, then run TBS again:

  1) Create a file named .env next to this project:
         {env_path}
     containing the single line:
         ANTHROPIC_API_KEY=sk-ant-...

  2) Or set it for this PowerShell session:
         $env:ANTHROPIC_API_KEY = "sk-ant-..."

Get a key at https://console.anthropic.com/settings/keys
""".strip()


def get_api_key() -> str:
    """Return the Anthropic API key, or raise ConfigError with instructions."""
    load_dotenv()
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise ConfigError(_API_KEY_HELP.format(env_path=ENV_CANDIDATES[0]))
    return key


def thinking_param() -> dict:
    """Translate THINKING_MODE into the API's `thinking` parameter."""
    if THINKING_MODE in ("adaptive", "on", "true", "1", "yes"):
        return {"type": "adaptive"}
    return {"type": "disabled"}


def ensure_data_dir() -> Path:
    """Create the local data directory if it does not exist yet."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
