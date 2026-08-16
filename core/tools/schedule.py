"""A small local calendar: recurring classes, one-off deadlines and events.

Backed by a plain JSON file at <project>/data/schedule.json so it is easy to
inspect, edit by hand, or back up.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import config

ENTRY_TYPES = ("class", "deadline", "event")

DAYS = {
    "monday": 0, "mon": 0, "m": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "t": 1,
    "wednesday": 2, "wed": 2, "w": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "th": 3, "r": 3,
    "friday": 4, "fri": 4, "f": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


TOOLS = [
    {
        "name": "schedule_view",
        "description": (
            "Read the user's local schedule: recurring classes, upcoming "
            "deadlines, and one-off events. Use for 'what do I have today', "
            "'when is my next class', 'what's due this week'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["today", "tomorrow", "week", "classes", "deadlines", "all"],
                    "description": "Which slice of the schedule to read. Default 'today'.",
                }
            },
            "required": ["scope"],
        },
    },
    {
        "name": "schedule_add",
        "description": (
            "Add something to the schedule. Use type 'class' with a day and "
            "time for a recurring course, 'deadline' with a date for "
            "assignments and exams, and 'event' with a date for one-off things."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(ENTRY_TYPES)},
                "title": {"type": "string", "description": "e.g. 'GB 110 Business Fundamentals'."},
                "day": {
                    "type": "string",
                    "description": "Weekday for a recurring class, e.g. 'Monday'. Classes only.",
                },
                "date": {
                    "type": "string",
                    "description": "Calendar date as YYYY-MM-DD. Deadlines and events only.",
                },
                "time": {"type": "string", "description": "Time as HH:MM in 24-hour form."},
                "location": {"type": "string", "description": "Room or building."},
                "notes": {"type": "string", "description": "Anything else worth remembering."},
            },
            "required": ["type", "title"],
        },
    },
    {
        "name": "schedule_remove",
        "description": "Remove a schedule entry by its id (ids are shown by schedule_view).",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "The entry id to delete."}
            },
            "required": ["id"],
        },
    },
]


# ---------------------------------------------------------------- storage --

def _load() -> dict:
    path = config.SCHEDULE_PATH
    if not path.is_file():
        return {"next_id": 1, "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"next_id": 1, "entries": []}
    if not isinstance(data, dict):
        return {"next_id": 1, "entries": []}
    data.setdefault("entries", [])
    data.setdefault("next_id", max((e.get("id", 0) for e in data["entries"]), default=0) + 1)
    return data


def _save(data: dict) -> None:
    config.ensure_data_dir()
    config.SCHEDULE_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ------------------------------------------------------------- formatting --

def _describe(entry: dict) -> str:
    bits = [f"[{entry.get('id')}]"]
    kind = entry.get("type", "event")
    bits.append(f"{kind}:")
    bits.append(entry.get("title", "(untitled)"))
    if kind == "class" and entry.get("day"):
        bits.append(f"— {entry['day']}s")
    elif entry.get("date"):
        bits.append(f"— {entry['date']}")
    if entry.get("time"):
        bits.append(f"at {entry['time']}")
    if entry.get("location"):
        bits.append(f"in {entry['location']}")
    if entry.get("notes"):
        bits.append(f"({entry['notes']})")
    return " ".join(bits)


def _parse_date(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def _entries_for_date(entries: list[dict], target: date) -> list[dict]:
    weekday_name = DAY_NAMES[target.weekday()]
    out = []
    for entry in entries:
        if entry.get("type") == "class":
            if str(entry.get("day", "")).lower().startswith(weekday_name.lower()[:3]):
                out.append(entry)
        else:
            parsed = _parse_date(str(entry.get("date", "")))
            if parsed == target:
                out.append(entry)
    return sorted(out, key=lambda e: str(e.get("time") or "99:99"))


# ------------------------------------------------------------- operations --

def schedule_view(tool_input: dict) -> str:
    scope = str(tool_input.get("scope", "today")).lower()
    data = _load()
    entries = data["entries"]
    if not entries:
        return "The schedule is empty. Nothing has been added yet."

    today = date.today()

    if scope == "today":
        hits = _entries_for_date(entries, today)
        header = f"Today, {today:%A %B %d}"
    elif scope == "tomorrow":
        target = today + timedelta(days=1)
        hits = _entries_for_date(entries, target)
        header = f"Tomorrow, {target:%A %B %d}"
    elif scope == "week":
        lines = [f"The next 7 days (from {today:%A %B %d}):"]
        any_hit = False
        for offset in range(7):
            target = today + timedelta(days=offset)
            hits = _entries_for_date(entries, target)
            if hits:
                any_hit = True
                lines.append(f"\n{target:%A %B %d}:")
                lines.extend("  " + _describe(e) for e in hits)
        return "\n".join(lines) if any_hit else "Nothing scheduled in the next 7 days."
    elif scope == "classes":
        hits = [e for e in entries if e.get("type") == "class"]
        hits.sort(key=lambda e: (DAYS.get(str(e.get("day", "")).lower(), 9), str(e.get("time") or "")))
        header = "Recurring classes"
    elif scope == "deadlines":
        hits = [e for e in entries if e.get("type") == "deadline"]
        hits.sort(key=lambda e: str(e.get("date") or "9999"))
        header = "Deadlines"
    else:  # all
        hits = entries
        header = "Everything on the schedule"

    if not hits:
        return f"{header}: nothing scheduled."
    return header + ":\n" + "\n".join(_describe(e) for e in hits)


def schedule_add(tool_input: dict) -> str:
    kind = str(tool_input.get("type", "event")).lower()
    if kind not in ENTRY_TYPES:
        return f"Unknown type '{kind}'. Use one of: {', '.join(ENTRY_TYPES)}."
    title = str(tool_input.get("title", "")).strip()
    if not title:
        return "An entry needs a title."

    entry: dict = {"type": kind, "title": title}

    if kind == "class":
        day_raw = str(tool_input.get("day", "")).strip().lower()
        if not day_raw:
            return "A recurring class needs a day of the week."
        if day_raw not in DAYS:
            return f"'{day_raw}' is not a weekday I recognise."
        entry["day"] = DAY_NAMES[DAYS[day_raw]]
    else:
        date_raw = str(tool_input.get("date", "")).strip()
        if not date_raw:
            return f"A {kind} needs a date (YYYY-MM-DD)."
        parsed = _parse_date(date_raw)
        if parsed is None:
            return f"'{date_raw}' is not a date I can read. Use YYYY-MM-DD."
        entry["date"] = parsed.isoformat()

    for optional in ("time", "location", "notes"):
        value = str(tool_input.get(optional, "") or "").strip()
        if value:
            entry[optional] = value

    data = _load()
    entry["id"] = data["next_id"]
    data["next_id"] += 1
    data["entries"].append(entry)
    try:
        _save(data)
    except OSError as exc:
        return f"Could not write the schedule file: {exc}"
    return "Added to the schedule: " + _describe(entry)


def schedule_remove(tool_input: dict) -> str:
    try:
        entry_id = int(tool_input.get("id"))
    except (TypeError, ValueError):
        return "A numeric entry id is required."
    data = _load()
    remaining = [e for e in data["entries"] if e.get("id") != entry_id]
    if len(remaining) == len(data["entries"]):
        return f"No schedule entry with id {entry_id}."
    removed = next(e for e in data["entries"] if e.get("id") == entry_id)
    data["entries"] = remaining
    try:
        _save(data)
    except OSError as exc:
        return f"Could not write the schedule file: {exc}"
    return "Removed: " + _describe(removed)


def today_summary() -> str:
    """Used by the brain to give TBS same-day context at session start."""
    data = _load()
    hits = _entries_for_date(data["entries"], date.today())
    if not hits:
        return ""
    return "; ".join(
        f"{e.get('title')}"
        + (f" at {e['time']}" if e.get("time") else "")
        + (f" in {e['location']}" if e.get("location") else "")
        for e in hits
    )


def handle(name: str, tool_input: dict) -> str:
    return HANDLERS[name](tool_input)


HANDLERS = {
    "schedule_view": schedule_view,
    "schedule_add": schedule_add,
    "schedule_remove": schedule_remove,
}
