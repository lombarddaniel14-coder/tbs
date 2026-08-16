"""Launch and close Windows applications, and open URLs in the browser."""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from urllib.parse import urlparse

# Friendly name -> what to actually run. Anything not listed here is passed
# through to the shell's `start` handler, which resolves App Paths entries,
# Store apps registered as commands, and executables on PATH.
KNOWN_APPS: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "files": "explorer.exe",
    "task manager": "taskmgr.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "terminal": "wt.exe",
    "settings": "ms-settings:",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe",
    "spotify": "spotify.exe",
    "discord": "discord.exe",
    "steam": "steam.exe",
    "vscode": "code.cmd",
    "vs code": "code.cmd",
    "visual studio code": "code.cmd",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "outlook": "outlook.exe",
    "obs": "obs64.exe",
}

# Friendly name -> image name for taskkill.
PROCESS_NAMES: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "CalculatorApp.exe",
    "calc": "CalculatorApp.exe",
    "paint": "mspaint.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe",
    "spotify": "Spotify.exe",
    "discord": "Discord.exe",
    "steam": "steam.exe",
    "vscode": "Code.exe",
    "vs code": "Code.exe",
    "visual studio code": "Code.exe",
    "word": "WINWORD.EXE",
    "excel": "EXCEL.EXE",
    "powerpoint": "POWERPNT.EXE",
    "outlook": "OUTLOOK.EXE",
    "terminal": "WindowsTerminal.exe",
    "powershell": "powershell.exe",
    "cmd": "cmd.exe",
    "obs": "obs64.exe",
}

# Never let the model kill these — doing so breaks the desktop session.
PROTECTED = {
    "explorer.exe",
    "csrss.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
    "system",
    "wininit.exe",
    "python.exe",
    "pythonw.exe",
}

TOOLS = [
    {
        "name": "open_app",
        "description": (
            "Launch a Windows application by name, for example 'spotify', "
            "'chrome', 'notepad', 'vs code'. Use this when the user asks to "
            "open, launch, start, or pull up a program."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "App name as a person would say it, e.g. 'spotify'.",
                },
                "arguments": {
                    "type": "string",
                    "description": "Optional arguments or a file path to open with it.",
                },
            },
            "required": ["app"],
        },
    },
    {
        "name": "close_app",
        "description": (
            "Close a running Windows application by name. Use when the user "
            "asks to close, quit, kill, or shut down a program."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "App name, e.g. 'spotify' or 'chrome'.",
                },
                "force": {
                    "type": "boolean",
                    "description": "Force-kill instead of asking it to close politely.",
                },
            },
            "required": ["app"],
        },
    },
    {
        "name": "open_url",
        "description": (
            "Open a web page in the default browser. Use for 'pull up X', "
            "'open youtube', 'take me to the Bentley portal', and similar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL. https:// is added if the scheme is missing.",
                }
            },
            "required": ["url"],
        },
    },
]


def _resolve_command(app: str) -> str:
    key = app.strip().lower()
    if key in KNOWN_APPS:
        return KNOWN_APPS[key]
    return app.strip()


def open_app(tool_input: dict) -> str:
    app = str(tool_input.get("app", "")).strip()
    if not app:
        return "No application name was given."
    args = str(tool_input.get("arguments", "") or "").strip()
    command = _resolve_command(app)

    # Protocol handlers (ms-settings:, etc.) go straight to the shell.
    if command.endswith(":") or "://" in command:
        try:
            os.startfile(command)  # type: ignore[attr-defined]
            return f"Opened {app}."
        except OSError as exc:
            return f"Could not open {app}: {exc}"

    # Prefer a direct launch when we can resolve the executable on PATH.
    resolved = shutil.which(command)
    try:
        if resolved:
            argv = [resolved] + ([args] if args else [])
            subprocess.Popen(argv, close_fds=True)
        else:
            # `start` resolves App Paths and registered Store apps. The empty
            # string is the window title `start` expects as its first argument.
            quoted = f'start "" "{command}"' + (f' "{args}"' if args else "")
            subprocess.Popen(quoted, shell=True)
        return f"Launched {app}."
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Could not launch {app}: {exc}"


def close_app(tool_input: dict) -> str:
    app = str(tool_input.get("app", "")).strip()
    if not app:
        return "No application name was given."
    key = app.lower()
    image = PROCESS_NAMES.get(key, app if app.lower().endswith(".exe") else app + ".exe")

    if image.lower() in PROTECTED:
        return (
            f"Refusing to close {image} — it is a protected system process. "
            "Closing it would destabilise the desktop session."
        )

    argv = ["taskkill", "/IM", image, "/T"]
    if tool_input.get("force"):
        argv.append("/F")
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Could not close {app}: {exc}"

    if result.returncode == 0:
        return f"Closed {app}."
    message = (result.stdout or result.stderr or "").strip()
    if "not found" in message.lower():
        return f"{app} does not appear to be running."
    if "access is denied" in message.lower():
        return f"Access denied closing {app}. Try again with force set to true."
    return f"Could not close {app}: {message or 'unknown error'}"


def open_url(tool_input: dict) -> str:
    url = str(tool_input.get("url", "")).strip()
    if not url:
        return "No URL was given."
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Refusing to open a '{parsed.scheme}' URL. Only http and https are allowed."
    if not parsed.netloc:
        return f"'{url}' does not look like a valid web address."
    try:
        webbrowser.open(url)
    except Exception as exc:  # pragma: no cover - browser registration issues
        return f"Could not open {url}: {exc}"
    return f"Opened {parsed.netloc} in the browser."


def handle(name: str, tool_input: dict) -> str:
    return HANDLERS[name](tool_input)


HANDLERS = {
    "open_app": open_app,
    "close_app": close_app,
    "open_url": open_url,
}
