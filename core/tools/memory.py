"""Persistent facts about the user, stored locally as JSON.

Everything in here is injected into the system prompt at the start of each
session, so TBS carries context between runs.
"""

from __future__ import annotations

import json
from datetime import datetime

import config

MAX_FACTS = 300
MAX_VALUE_CHARS = 500


TOOLS = [
    {
        "name": "remember",
        "description": (
            "Store a durable fact about the user so it is available in future "
            "sessions — preferences, people, courses, goals, routines, "
            "logins-free settings. Do not store passwords, card numbers, or "
            "anything secret. Storing the same topic again overwrites it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Short key, e.g. 'major' or 'roommate' or 'gym schedule'.",
                },
                "fact": {
                    "type": "string",
                    "description": "The fact itself, written as a full sentence.",
                },
            },
            "required": ["topic", "fact"],
        },
    },
    {
        "name": "recall",
        "description": (
            "Look up stored facts about the user. Optionally filter by a search "
            "term; with no term, returns everything remembered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Optional filter text."}
            },
        },
    },
    {
        "name": "forget",
        "description": "Delete a stored fact by its topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "The topic key to delete."}
            },
            "required": ["topic"],
        },
    },
]

# Refuse to persist anything that looks like a credential.
_BANNED_TOPIC_WORDS = {"password", "passcode", "pin", "ssn", "credit card", "cvv", "api key", "secret key"}


def _load() -> dict:
    path = config.MEMORY_PATH
    if not path.is_file():
        return {"facts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"facts": {}}
    if not isinstance(data, dict) or not isinstance(data.get("facts"), dict):
        return {"facts": {}}
    return data


def _save(data: dict) -> None:
    config.ensure_data_dir()
    config.MEMORY_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def remember(tool_input: dict) -> str:
    topic = str(tool_input.get("topic", "")).strip().lower()
    fact = str(tool_input.get("fact", "")).strip()
    if not topic or not fact:
        return "Both a topic and a fact are required."
    if any(word in topic for word in _BANNED_TOPIC_WORDS):
        return (
            "I will not store credentials or secrets in a plain-text file. "
            "Use a password manager for that."
        )
    if len(fact) > MAX_VALUE_CHARS:
        fact = fact[:MAX_VALUE_CHARS].rstrip() + "..."

    data = _load()
    facts = data["facts"]
    if topic not in facts and len(facts) >= MAX_FACTS:
        return f"Memory is full ({MAX_FACTS} facts). Forget something first."
    existed = topic in facts
    facts[topic] = {"fact": fact, "updated": datetime.now().isoformat(timespec="seconds")}
    try:
        _save(data)
    except OSError as exc:
        return f"Could not write the memory file: {exc}"
    return f"{'Updated' if existed else 'Noted'}: {topic} — {fact}"


def recall(tool_input: dict) -> str:
    search = str((tool_input or {}).get("search", "") or "").strip().lower()
    facts = _load()["facts"]
    if not facts:
        return "Nothing has been stored in memory yet."
    items = sorted(facts.items())
    if search:
        items = [(k, v) for k, v in items if search in k or search in str(v.get("fact", "")).lower()]
        if not items:
            return f"Nothing in memory matches '{search}'."
    return "\n".join(f"- {topic}: {value.get('fact')}" for topic, value in items)


def forget(tool_input: dict) -> str:
    topic = str(tool_input.get("topic", "")).strip().lower()
    if not topic:
        return "A topic is required."
    data = _load()
    if topic not in data["facts"]:
        return f"There is nothing stored under '{topic}'."
    removed = data["facts"].pop(topic)
    try:
        _save(data)
    except OSError as exc:
        return f"Could not write the memory file: {exc}"
    return f"Forgotten: {topic} — {removed.get('fact')}"


def facts_block() -> str:
    """Render all stored facts for injection into the system prompt."""
    facts = _load()["facts"]
    if not facts:
        return ""
    lines = [f"- {topic}: {value.get('fact')}" for topic, value in sorted(facts.items())]
    return "\n".join(lines)


def handle(name: str, tool_input: dict) -> str:
    return HANDLERS[name](tool_input)


HANDLERS = {
    "remember": remember,
    "recall": recall,
    "forget": forget,
}
