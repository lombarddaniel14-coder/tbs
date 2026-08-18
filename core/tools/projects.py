"""Project awareness — lets TBS answer questions about the user's real work.

Reads his Obsidian vault and code folders and reports status out loud. Every
answer is grounded in a file on disk; nothing here guesses.

SAFETY: this module is STRICTLY READ-ONLY over the user's roots. A misheard
voice command must never be able to modify his files. All filesystem access
goes through `_safe_read()`, which resolves the path and refuses anything
outside ALLOWED_ROOTS. The only path this module ever writes is the index
cache inside TBS's own data directory.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import config

# --------------------------------------------------------------------------
# Where TBS is allowed to look
# --------------------------------------------------------------------------

HOME = Path.home()

# Set VAULT_ROOT to the absolute path of the Obsidian vault. Falls back to the
# first "OneDrive*" root under home, then to ~/Claude Data.
def _default_vault_root() -> Path:
    for child in sorted(HOME.glob("OneDrive*")):
        if (child / "Claude Data").is_dir():
            return child / "Claude Data"
    return HOME / "Claude Data"


VAULT_ROOT = Path(os.environ.get("VAULT_ROOT") or _default_vault_root())

# Active builds live INSIDE the vault under Projects\ (five sections since
# 2026-08-13: Main Business, Side Business, The Buddy System, Tools,
# Side Projects). VAULT_ROOT already covers the code; kept for readability.
CODE_ROOT = VAULT_ROOT / "Projects"

# Solo Leveling moved under Side Projects in the 2026-08-13 rebuild.
SL_ROOT = CODE_ROOT / "Side Projects" / "Solo Leveling Game"

# Claude Code's working directory. Not a storage location — nothing should
# accumulate here — but stay readable so anything in flight is still visible.
WORKSPACE_ROOT = HOME / "Claude" / "Projects"

ALLOWED_ROOTS = (VAULT_ROOT, SL_ROOT, WORKSPACE_ROOT)

INDEX_PATH = config.DATA_DIR / "projects-index.json"
DIGEST_PATH = config.DATA_DIR / "projects-digest.md"

INDEX_MAX_AGE_HOURS = 24

# Directories that are never projects and never worth walking.
SKIP_DIRS = {
    ".git", ".obsidian", ".trash", "node_modules", "__pycache__", ".venv",
    "venv", "voices", "entries", "dist", "build", ".pytest_cache", ".idea",
    ".vscode", "site-packages", ".claude",
}

# A directory is a "project" if it contains one of these.
PROJECT_MARKERS = ("README.md", "HOME.md", "index.html")

# Files most likely to state a project's status, in priority order.
STATUS_FILE_PATTERNS = (
    "readme.md", "home.md", "where we're at", "status", "profile",
    "plan brief", "overview",
)

MAX_DEPTH = 3
MAX_FILE_HEAD = 6000       # chars read from any single file
MAX_REPLY_CHARS = 1500     # spoken answers must stay short
MAX_SCAN_SECONDS = 8.0     # hard bound on a live scan
MAX_LOOP_FILES = 40        # markdown files scanned per project for open loops

# Folder names that are code-layout noise, not projects the user thinks about.
NOT_PROJECTS = {
    "core", "tools", "data", "src", "lib", "tests", "test", "docs", "assets",
    "personality", "research", "scripts", "static", "public", "images",
    "product-images", "creatives", "skills", "attachments",
}

_OPEN_LOOP_RE = re.compile(r"^\s*[-*]\s*\[ \]\s*(.+)$")
_BLOCKED_RE = re.compile(r"\b(TODO|BLOCKED|pending|waiting on|not yet|unresolved)\b", re.I)
_STATUS_RE = re.compile(r"^status:\s*(.+)$", re.I | re.M)

_DONE_WORDS = ("done", "complete", "completed", "delivered", "shipped", "live", "sent", "closed")


class AccessDenied(RuntimeError):
    """Raised when something tries to read outside the allowed roots."""


# --------------------------------------------------------------------------
# The read-only guard — every file access goes through here
# --------------------------------------------------------------------------

def _assert_readable_path(path: Path) -> Path:
    """Resolve `path` and confirm it sits inside an allowed root.

    Raises AccessDenied otherwise. This is the single chokepoint; nothing in
    this module opens a file without calling it first.
    """
    try:
        resolved = Path(path).resolve()
    except OSError as exc:
        raise AccessDenied(f"Cannot resolve {path}: {exc}") from exc

    for root in ALLOWED_ROOTS:
        try:
            resolved.relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        return resolved

    raise AccessDenied(
        f"Refusing to read outside the allowed folders: {resolved}"
    )


def _safe_read(path: Path, limit: int = MAX_FILE_HEAD) -> str:
    """Read the head of a text file, or return '' if it isn't readable text."""
    resolved = _assert_readable_path(path)
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except (OSError, UnicodeDecodeError):
        # OneDrive placeholder not downloaded, permission denied, or binary.
        return ""


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def _looks_like_project(entry_path: Path, names: set[str]) -> bool:
    if entry_path.name.lower() in NOT_PROJECTS:
        return False
    if any(marker in names for marker in PROJECT_MARKERS):
        return True
    folder = entry_path.name.lower()
    return any(
        n.lower().endswith(".md") and folder in n.lower()
        for n in names
    )


def _collect_loops(project: Path, deadline: float) -> list[str]:
    """Scan the markdown in a project (depth 2) for unchecked work."""
    loops: list[str] = []
    scanned = 0
    stack: list[tuple[Path, int]] = [(project, 0)]

    while stack and scanned < MAX_LOOP_FILES:
        if time.monotonic() > deadline:
            break
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if scanned >= MAX_LOOP_FILES:
                break
            if entry.is_dir(follow_symlinks=False):
                if depth < 2 and entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                    stack.append((Path(entry.path), depth + 1))
                continue
            if not entry.name.lower().endswith(".md"):
                continue
            text = _safe_read(Path(entry.path))
            if not text:
                continue
            scanned += 1
            for item in _open_loops(text):
                loops.append(f"{item}  ({entry.name[:-3]})")
    return loops


def _iter_project_dirs(root: Path, deadline: float):
    """Yield directories under `root` that look like projects."""
    if not root.is_dir():
        return

    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        if time.monotonic() > deadline:
            return
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue

        names = {e.name for e in entries if e.is_file()}
        if depth > 0 and _looks_like_project(current, names):
            yield current
            # Still descend one more level — nested projects are common.

        if depth >= MAX_DEPTH:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            stack.append((Path(entry.path), depth + 1))


def _status_files(project: Path) -> list[Path]:
    try:
        files = [Path(e.path) for e in os.scandir(project) if e.is_file()]
    except OSError:
        return []
    scored: list[tuple[int, Path]] = []
    for path in files:
        low = path.name.lower()
        if not low.endswith((".md", ".html")):
            continue
        for rank, pattern in enumerate(STATUS_FILE_PATTERNS):
            if pattern in low:
                scored.append((rank, path))
                break
    scored.sort(key=lambda pair: pair[0])
    return [path for _, path in scored[:3]]


def _summarise(text: str, limit: int = 300) -> str:
    """First real prose line(s) of a markdown file."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "---", "|", ">", "!", "```", "tags:")):
            continue
        if line.startswith(("- ", "* ", "1. ")):
            line = line[2:].strip()
        line = re.sub(r"[*_`\[\]]", "", line)
        if len(line) > 25:
            return line[:limit].rstrip()
    return ""


def _open_loops(text: str) -> list[str]:
    loops: list[str] = []
    for raw in text.splitlines():
        match = _OPEN_LOOP_RE.match(raw)
        if match:
            item = re.sub(r"[*_`\[\]]", "", match.group(1)).strip()
            if item:
                loops.append(item)
        elif _BLOCKED_RE.search(raw) and len(raw.strip()) < 200:
            cleaned = re.sub(r"[*_`#\[\]]", "", raw).strip()
            if cleaned:
                loops.append(cleaned)
    return loops


def _declared_status(text: str) -> str:
    match = _STATUS_RE.search(text)
    if not match:
        return ""
    return re.sub(r"[*_`]", "", match.group(1)).strip()[:120]


def scan(deadline_seconds: float = MAX_SCAN_SECONDS) -> list[dict]:
    """Walk the allowed roots and build a project record for each hit."""
    deadline = time.monotonic() + deadline_seconds
    projects: list[dict] = []
    seen: set[str] = set()

    roots = [VAULT_ROOT, CODE_ROOT]
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(_iter_project_dirs(root, deadline))
    if SL_ROOT.is_dir():
        candidates.append(SL_ROOT)

    for project in candidates:
        key = str(project).lower()
        if key in seen:
            continue
        seen.add(key)

        try:
            mtime = project.stat().st_mtime
        except OSError:
            continue

        blob = ""
        status = ""
        for status_file in _status_files(project):
            text = _safe_read(status_file)
            if not text:
                continue
            blob += text + "\n"
            status = status or _declared_status(text)

        loops = _collect_loops(project, deadline)
        projects.append(
            {
                "name": project.name,
                "path": str(project),
                "modified": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                "modified_ts": mtime,
                "status": status,
                "summary": _summarise(blob),
                "open_loops": loops[:12],
                "open_loop_count": len(loops),
            }
        )

    projects.sort(key=lambda p: p["modified_ts"], reverse=True)
    return projects


# --------------------------------------------------------------------------
# Index cache (the ONLY thing this module writes)
# --------------------------------------------------------------------------

def save_index(projects: list[dict]) -> Path:
    config.ensure_data_dir()
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "projects": projects,
    }
    INDEX_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return INDEX_PATH


def load_index(max_age_hours: int = INDEX_MAX_AGE_HOURS) -> list[dict] | None:
    if not INDEX_PATH.is_file():
        return None
    try:
        payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(payload["generated"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if datetime.now() - generated > timedelta(hours=max_age_hours):
        return None
    projects = payload.get("projects")
    return projects if isinstance(projects, list) else None


def get_projects(force_scan: bool = False) -> list[dict]:
    if not force_scan:
        cached = load_index()
        if cached:
            return cached
    projects = scan()
    try:
        save_index(projects)
    except OSError:
        pass  # a read-only run is still useful
    return projects


# --------------------------------------------------------------------------
# Matching and formatting
# --------------------------------------------------------------------------

_ALIASES = {
    "the business": "ai mentorship",
    "business": "ai mentorship",
    "mentorship": "ai mentorship",
    "the app": "solo leveling",
    "sl": "solo leveling",
    "tracker": "solo leveling",
    "tbs": "tbs",
    "recorder": "lecture recorder",
    "school": "school",
}


def _match(name: str, projects: list[dict]) -> list[dict]:
    query = (name or "").strip().lower()
    if not query:
        return []
    query = _ALIASES.get(query, query)
    words = [w for w in re.split(r"\W+", query) if len(w) > 2]

    exact = [p for p in projects if p["name"].lower() == query]
    if exact:
        return exact
    contains = [p for p in projects if query in p["name"].lower() or query in p["path"].lower()]
    if contains:
        return contains
    if not words:
        return []
    scored = []
    for project in projects:
        haystack = (project["name"] + " " + project["path"]).lower()
        hits = sum(1 for w in words if w in haystack)
        if hits:
            scored.append((hits, project))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [p for _, p in scored]


def _spoken_date(iso: str) -> str:
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return "an unknown date"
    days = (datetime.now() - when).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return when.strftime("%B %-d") if os.name != "nt" else when.strftime("%B %d").replace(" 0", " ")


# Typographic characters that are common in the vault, unpronounceable by TTS,
# and un-encodable by the default Windows console codepage.
_SPEECH_SUBS = {
    "→": " to ", "←": " from ", "↔": " to ",
    "—": " - ", "–": " - ", "…": "...",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "·": ".", "•": "-", " ": " ", "✓": "done",
    "⚠": "warning", "✅": "done", "❌": "not done",
}


def _speakable(text: str) -> str:
    """Make text safe to print on a Windows console and sane to read aloud."""
    for char, replacement in _SPEECH_SUBS.items():
        text = text.replace(char, replacement)
    # Drop emoji and anything else outside Latin-1; they are noise when spoken.
    text = "".join(ch for ch in text if ord(ch) < 256 or ch.isalnum())
    return re.sub(r"[ \t]{2,}", " ", text)


def _cap(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    text = _speakable(text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for stop in (". ", "\n"):
        idx = cut.rfind(stop)
        if idx > limit * 0.6:
            return cut[: idx + 1].strip()
    return cut.rstrip() + "..."


# --------------------------------------------------------------------------
# Tool schemas
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_projects",
        "description": (
            "List the user's real projects — from his Obsidian vault and his code "
            "folders — most recently worked on first. Use when he asks what he's "
            "working on, what projects exist, or what he's been up to."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many to return. Default 10."}
            },
        },
    },
    {
        "name": "project_status",
        "description": (
            "Read the current state of one project from its actual files and "
            "report it. Accepts loose names — 'the business', 'AI mentorship', "
            "'solo leveling', 'lecture recorder' all work. Use this instead of "
            "guessing whenever he asks how something is going."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name, however he said it."}
            },
            "required": ["name"],
        },
    },
    {
        "name": "project_search",
        "description": (
            "Full-text search across all of the user's notes and project files. "
            "Use for specific questions the other tools can't answer — a person's "
            "name, a price, a link, a decision he wrote down somewhere."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "limit": {"type": "integer", "description": "Max matches. Default 8."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "whats_blocking",
        "description": (
            "Find open loops across his work — unchecked checkboxes, TODOs, things "
            "marked pending or blocked. Optionally narrow to one project. Use when "
            "he asks what's left, what's blocking him, or what he should do next."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Optional project to narrow to."}
            },
        },
    },
    {
        "name": "recent_activity",
        "description": (
            "What the user has actually touched recently, grouped by project. Use "
            "when he asks what he did this week or what he was last working on."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Look back this many days. Default 7."}
            },
        },
    },
]


# --------------------------------------------------------------------------
# Handlers — all output is written to be spoken aloud
# --------------------------------------------------------------------------

def list_projects(tool_input: dict) -> str:
    limit = int(tool_input.get("limit") or 10)
    projects = get_projects()
    if not projects:
        return "I could not find any projects in the usual folders, sir."
    lines = []
    for project in projects[:limit]:
        when = _spoken_date(project["modified"])
        status = f" — {project['status']}" if project["status"] else ""
        lines.append(f"{project['name']}, last touched {when}{status}.")
    header = f"{len(projects)} projects. The most recent:\n"
    return _cap(header + "\n".join(lines))


def project_status(tool_input: dict) -> str:
    name = str(tool_input.get("name", "")).strip()
    if not name:
        return "Which project, sir?"
    projects = get_projects()
    matches = _match(name, projects)
    if not matches:
        return f"I have nothing on file for '{name}', sir."

    project = matches[0]
    parts = [f"{project['name']}."]
    if project["status"]:
        parts.append(f"Status: {project['status']}.")
    if project["summary"]:
        parts.append(project["summary"])
    parts.append(f"Last touched {_spoken_date(project['modified'])}.")
    if project["open_loop_count"]:
        parts.append(f"{project['open_loop_count']} open items.")
        for item in project["open_loops"][:4]:
            parts.append(f"— {item}")
    else:
        parts.append("Nothing marked open.")
    if len(matches) > 1:
        others = ", ".join(p["name"] for p in matches[1:4])
        parts.append(f"(Also matched: {others}.)")
    return _cap(" ".join(parts[:3]) + "\n" + "\n".join(parts[3:]))


def project_search(tool_input: dict) -> str:
    query = str(tool_input.get("query", "")).strip()
    limit = int(tool_input.get("limit") or 8)
    if not query:
        return "What should I search for, sir?"

    needle = query.lower()
    hits: list[str] = []
    deadline = time.monotonic() + MAX_SCAN_SECONDS

    for root in (VAULT_ROOT, CODE_ROOT):
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() > deadline or len(hits) >= limit:
                break
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for filename in filenames:
                if not filename.lower().endswith((".md", ".txt")):
                    continue
                path = Path(dirpath) / filename
                text = _safe_read(path)
                if needle not in text.lower():
                    continue
                for line in text.splitlines():
                    if needle in line.lower():
                        cleaned = re.sub(r"[*_`#\[\]]", "", line).strip()
                        if cleaned:
                            hits.append(f"{filename}: {cleaned[:180]}")
                            break
                if len(hits) >= limit:
                    break

    if not hits:
        return f"Nothing in your files mentions '{query}', sir."
    return _cap(f"Found {len(hits)} mentions of '{query}':\n" + "\n".join(hits))


def whats_blocking(tool_input: dict) -> str:
    name = str((tool_input or {}).get("name", "") or "").strip()
    projects = get_projects()
    if name:
        projects = _match(name, projects)
        if not projects:
            return f"I have nothing on file for '{name}', sir."

    blocking = [p for p in projects if p["open_loop_count"]]
    if not blocking:
        return "Nothing is marked as open, sir. Which either means you are done, or that you have stopped ticking boxes."

    blocking.sort(key=lambda p: p["open_loop_count"], reverse=True)
    lines = []
    total = 0
    for project in blocking[:5]:
        total += project["open_loop_count"]
        lines.append(f"{project['name']}: {project['open_loop_count']} open.")
        for item in project["open_loops"][:3]:
            lines.append(f"  — {item}")
    header = f"{total} open items across {len(blocking)} projects.\n"
    return _cap(header + "\n".join(lines))


def recent_activity(tool_input: dict) -> str:
    days = int((tool_input or {}).get("days") or 7)
    cutoff = datetime.now() - timedelta(days=days)
    projects = get_projects()
    recent = [
        p for p in projects
        if datetime.fromisoformat(p["modified"]) >= cutoff
    ]
    if not recent:
        return f"Nothing has changed in the last {days} days, sir."
    lines = [f"{p['name']}, {_spoken_date(p['modified'])}." for p in recent[:10]]
    return _cap(f"{len(recent)} projects touched in the last {days} days:\n" + "\n".join(lines))


def handle(name: str, tool_input: dict) -> str:
    return HANDLERS[name](tool_input)


HANDLERS = {
    "list_projects": list_projects,
    "project_status": project_status,
    "project_search": project_search,
    "whats_blocking": whats_blocking,
    "recent_activity": recent_activity,
}
