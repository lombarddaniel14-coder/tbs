"""Find and open files on the local machine."""

from __future__ import annotations

import os
import time
from pathlib import Path

# Windows "known folders" can be redirected (OneDrive Known Folder Move puts
# the real Desktop inside a OneDrive root, leaving ~\Desktop nonexistent), so
# resolve each one instead of assuming it sits directly under the home dir.
# Since 2026-08-13 the known folders live in the Bentley OneDrive; the old
# personal ~\OneDrive root is dead but may leave empty shell folders behind,
# so the Bentley root must be checked first.
def _known_folder(name: str) -> Path:
    for candidate in (
        Path.home() / "OneDrive - Bentley University" / name,
        Path.home() / "OneDrive" / name,
        Path.home() / name,
    ):
        if candidate.is_dir():
            return candidate
    return Path.home() / name  # nonexistent; callers filter with is_dir()


# Directories searched by default, in priority order.
DEFAULT_ROOTS = [
    _known_folder("Desktop"),
    _known_folder("Documents"),
    _known_folder("Downloads"),
    _known_folder("Pictures"),
    _known_folder("Videos"),
    _known_folder("Music"),
]

# Never descend into these — they are huge and never what the user meant.
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", "env",
    "AppData", "$RECYCLE.BIN", "System Volume Information", ".cache",
    "site-packages", "dist", "build", ".next", ".idea", ".vscode",
}

MAX_RESULTS = 25
MAX_SCANNED = 60_000  # hard ceiling so a bad query cannot hang the assistant


TOOLS = [
    {
        "name": "find_files",
        "description": (
            "Search the user's personal folders (Desktop, Documents, Downloads, "
            "Pictures, Videos, Music) for files whose name matches a query. Use "
            "this when the user asks where a file is, or asks to find a "
            "document, essay, screenshot, or download."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Part of the file name, e.g. 'econ essay' or 'resume'.",
                },
                "extension": {
                    "type": "string",
                    "description": "Optional extension filter, e.g. 'pdf' or 'docx'.",
                },
                "folder": {
                    "type": "string",
                    "description": (
                        "Optional folder to search instead of the defaults. "
                        "Accepts an absolute path or one of: desktop, documents, "
                        "downloads, pictures, videos, music."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "open_file",
        "description": (
            "Open a file or folder with its default Windows application. Pass "
            "the absolute path, usually one returned by find_files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file or folder."}
            },
            "required": ["path"],
        },
    },
]

_NAMED_FOLDERS = {
    "desktop": Path.home() / "Desktop",
    "documents": Path.home() / "Documents",
    "docs": Path.home() / "Documents",
    "downloads": Path.home() / "Downloads",
    "pictures": Path.home() / "Pictures",
    "photos": Path.home() / "Pictures",
    "videos": Path.home() / "Videos",
    "music": Path.home() / "Music",
    "home": Path.home(),
}


def _resolve_roots(folder: str) -> list[Path]:
    folder = (folder or "").strip()
    if not folder:
        return [p for p in DEFAULT_ROOTS if p.is_dir()]
    named = _NAMED_FOLDERS.get(folder.lower())
    if named is not None:
        return [named] if named.is_dir() else []
    candidate = Path(os.path.expandvars(folder)).expanduser()
    return [candidate] if candidate.is_dir() else []


def _walk(root: Path, terms: list[str], ext: str, budget: list[int]) -> list[Path]:
    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in filenames:
            budget[0] -= 1
            if budget[0] <= 0:
                return hits
            lower = filename.lower()
            if ext and not lower.endswith(ext):
                continue
            if all(term in lower for term in terms):
                hits.append(Path(dirpath) / filename)
                if len(hits) >= MAX_RESULTS * 4:
                    return hits
    return hits


def find_files(tool_input: dict) -> str:
    query = str(tool_input.get("query", "")).strip()
    if not query:
        return "No search query was given."
    terms = [t for t in query.lower().split() if t]
    ext = str(tool_input.get("extension", "") or "").strip().lower().lstrip(".")
    ext = ("." + ext) if ext else ""

    roots = _resolve_roots(str(tool_input.get("folder", "") or ""))
    if not roots:
        return "That folder does not exist on this machine."

    budget = [MAX_SCANNED]
    hits: list[Path] = []
    for root in roots:
        hits.extend(_walk(root, terms, ext, budget))
        if budget[0] <= 0:
            break

    if not hits:
        where = "the usual folders" if len(roots) > 1 else str(roots[0])
        return f"No files matching '{query}' were found in {where}."

    # Most recently modified first — that is almost always what was meant.
    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    hits.sort(key=mtime, reverse=True)
    lines = [f"Found {len(hits)} match(es); showing up to {MAX_RESULTS}:"]
    for path in hits[:MAX_RESULTS]:
        try:
            stat = path.stat()
            size_kb = stat.st_size / 1024
            when = time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime))
            lines.append(f"- {path} ({size_kb:,.0f} KB, modified {when})")
        except OSError:
            lines.append(f"- {path}")
    if budget[0] <= 0:
        lines.append("(Search stopped early — the folder tree was very large.)")
    return "\n".join(lines)


def open_file(tool_input: dict) -> str:
    raw = str(tool_input.get("path", "")).strip().strip('"')
    if not raw:
        return "No path was given."
    path = Path(os.path.expandvars(raw)).expanduser()
    if not path.exists():
        return f"Nothing exists at {path}."
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except OSError as exc:
        return f"Could not open {path}: {exc}"
    return f"Opened {path.name}."


def handle(name: str, tool_input: dict) -> str:
    return HANDLERS[name](tool_input)


HANDLERS = {
    "find_files": find_files,
    "open_file": open_file,
}
