"""Build TBS's project index.

Walks the user's vault and code folders and writes two files into TBS's own
data directory:

    data\\projects-index.json   - what the `projects` tools read
    data\\projects-digest.md    - the same thing, readable by a human

Run it directly to refresh:

    py -3.11 core\\digest.py

Reading is strictly read-only over the user's folders; see tools/projects.py.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

import config  # noqa: E402
from tools import projects as P  # noqa: E402


def write_markdown(records: list[dict]) -> Path:
    config.ensure_data_dir()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_loops = sum(r["open_loop_count"] for r in records)

    lines = [
        "# Project Digest",
        "",
        f"Generated {now} · {len(records)} projects · {total_loops} open items",
        "",
    ]
    for record in records:
        lines.append(f"## {record['name']}")
        lines.append("")
        lines.append(f"- **Path:** `{record['path']}`")
        lines.append(f"- **Last touched:** {record['modified'][:10]}")
        if record["status"]:
            lines.append(f"- **Status:** {record['status']}")
        if record["summary"]:
            lines.append(f"- **Summary:** {record['summary']}")
        if record["open_loop_count"]:
            lines.append(f"- **Open items ({record['open_loop_count']}):**")
            for item in record["open_loops"][:8]:
                lines.append(f"  - {item}")
        lines.append("")

    P.DIGEST_PATH.write_text("\n".join(lines), encoding="utf-8")
    return P.DIGEST_PATH


def main() -> int:
    started = time.monotonic()
    print("Scanning...")
    records = P.scan()
    index_path = P.save_index(records)
    digest_path = write_markdown(records)
    elapsed = time.monotonic() - started

    total_loops = sum(r["open_loop_count"] for r in records)
    print(f"\n{len(records)} projects, {total_loops} open items, {elapsed:.1f}s")
    print(f"  {index_path}")
    print(f"  {digest_path}\n")

    for record in records[:15]:
        status = f"  [{record['status']}]" if record["status"] else ""
        loops = f"  ({record['open_loop_count']} open)" if record["open_loop_count"] else ""
        print(f"  {record['modified'][:10]}  {record['name']}{status}{loops}")
    if len(records) > 15:
        print(f"  ... and {len(records) - 15} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
