"""System telemetry: battery, CPU, RAM, disk, wifi.

Uses psutil when available. If psutil is missing, the tool still works for
battery/wifi/disk via Windows built-ins and reports the rest as unavailable.
"""

from __future__ import annotations

import shutil
import subprocess

try:
    import psutil  # type: ignore
except ImportError:  # graceful degradation
    psutil = None  # type: ignore


TOOLS = [
    {
        "name": "system_info",
        "description": (
            "Read live hardware and system status from this Windows laptop: "
            "battery percentage and charging state, CPU load, RAM usage, disk "
            "free space, and the current wifi network. Use this whenever the "
            "user asks how the machine is doing, how much battery is left, "
            "whether they are online, or how much space they have."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "what": {
                    "type": "string",
                    "enum": ["all", "battery", "cpu", "ram", "disk", "wifi"],
                    "description": "Which reading to take. 'all' returns everything.",
                }
            },
            "required": ["what"],
        },
    }
]


def _battery() -> str:
    if psutil is None:
        return "Battery: unavailable (psutil not installed)."
    try:
        batt = psutil.sensors_battery()
    except Exception as exc:  # pragma: no cover - platform quirks
        return f"Battery: unavailable ({exc})."
    if batt is None:
        return "Battery: no battery detected (desktop or driver not reporting)."
    percent = int(round(batt.percent))
    if batt.power_plugged:
        state = "plugged in and charging"
    else:
        state = "on battery"
    line = f"Battery: {percent} percent, {state}."
    secs = batt.secsleft
    if not batt.power_plugged and isinstance(secs, int) and secs > 0:
        hours, rem = divmod(secs, 3600)
        minutes = rem // 60
        line += f" Roughly {hours}h {minutes}m remaining."
    return line


def _cpu() -> str:
    if psutil is None:
        return "CPU: unavailable (psutil not installed)."
    percent = psutil.cpu_percent(interval=0.4)
    cores = psutil.cpu_count(logical=True)
    physical = psutil.cpu_count(logical=False)
    try:
        freq = psutil.cpu_freq()
        freq_txt = f" at {freq.current / 1000:.1f} GHz" if freq else ""
    except Exception:
        freq_txt = ""
    return (
        f"CPU: {percent:.0f} percent load across {cores} logical cores "
        f"({physical} physical){freq_txt}."
    )


def _ram() -> str:
    if psutil is None:
        return "RAM: unavailable (psutil not installed)."
    mem = psutil.virtual_memory()
    gb = 1024 ** 3
    return (
        f"RAM: {mem.percent:.0f} percent used — "
        f"{(mem.total - mem.available) / gb:.1f} GB of {mem.total / gb:.1f} GB, "
        f"{mem.available / gb:.1f} GB free."
    )


def _disk() -> str:
    lines = []
    if psutil is not None:
        try:
            parts = [p for p in psutil.disk_partitions(all=False) if p.fstype]
        except Exception:
            parts = []
        for part in parts:
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            gb = 1024 ** 3
            lines.append(
                f"{part.device.rstrip(chr(92))} {usage.free / gb:.0f} GB free "
                f"of {usage.total / gb:.0f} GB ({usage.percent:.0f} percent used)"
            )
    if not lines:
        usage = shutil.disk_usage("C:\\")
        gb = 1024 ** 3
        lines.append(
            f"C: {usage.free / gb:.0f} GB free of {usage.total / gb:.0f} GB"
        )
    return "Disk: " + "; ".join(lines) + "."


def _wifi() -> str:
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Wifi: unavailable ({exc})."

    fields: dict[str, str] = {}
    for raw in out.splitlines():
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        fields[key.strip().lower()] = value.strip()

    state = fields.get("state", "").lower()
    if not fields:
        return "Wifi: no wireless interface reported."
    if state and state != "connected":
        return f"Wifi: not connected (adapter state: {state})."
    ssid = fields.get("ssid") or "unknown network"
    signal = fields.get("signal", "")
    speed = fields.get("receive rate (mbps)", "")
    parts = [f"Wifi: connected to {ssid}"]
    if signal:
        parts.append(f"signal {signal}")
    if speed:
        parts.append(f"receive rate {speed} Mbps")
    return ", ".join(parts) + "."


_READERS = {
    "battery": _battery,
    "cpu": _cpu,
    "ram": _ram,
    "disk": _disk,
    "wifi": _wifi,
}


def system_info(tool_input: dict) -> str:
    what = str((tool_input or {}).get("what", "all")).lower()
    if what == "all":
        return "\n".join(reader() for reader in _READERS.values())
    reader = _READERS.get(what)
    if reader is None:
        return f"Unknown reading '{what}'. Valid: all, battery, cpu, ram, disk, wifi."
    return reader()


def handle(name: str, tool_input: dict) -> str:
    return HANDLERS[name](tool_input)


HANDLERS = {"system_info": system_info}
