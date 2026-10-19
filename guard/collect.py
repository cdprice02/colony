# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml==6.0.2"]
# ///
"""Draft Guard cases from real Home Assistant history.

Reads HA log files and the Repairs registry copied out of HA's config dir, keeps
WARNING and above, and writes one case per signature (logger plus message, numbers
stripped: Guard's dedupe key) with `expect: null`. Label each by hand; run.py
skips unlabelled cases. Re-running keeps the labels already given and every
signature from earlier runs, so history accumulates across HA restarts.

Usage:
  uv run guard/collect.py DIR [--out guard/cases/history.yaml]

DIR holds home-assistant.log* and repairs.issue_registry (either may be absent).
"""

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
# 2026-10-16 07:15:00.123 WARNING (MainThread) [homeassistant.components.x] message
LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ (WARNING|ERROR|CRITICAL|INFO|DEBUG) "
    r"\([^)]*\) \[([^\]]+)\] (.*)$"
)
KEEP = {"WARNING", "ERROR", "CRITICAL"}


def signature(source: str, text: str) -> str:
    return f"{source}|{re.sub(r'0x[0-9a-f]+|\d+', '#', text)}"


def read_logs(paths: list[Path]) -> list[dict]:
    records, current = [], None
    for path in paths:
        for line in path.read_text(errors="replace").splitlines():
            if m := LINE.match(line):
                current = None
                if m[2] in KEEP:
                    current = {
                        "at": m[1],
                        "level": m[2],
                        "source": m[3],
                        "text": m[4].strip(),
                        "tail": "",
                    }
                    records.append(current)
            elif current and line.strip():
                current["tail"] = (
                    line.strip()
                )  # a traceback's last line is the exception
    for r in records:
        if r["tail"] and r["tail"] not in r["text"]:
            r["text"] = f"{r['text']} ({r['tail']})"
    return records


def read_repairs(path: Path) -> list[dict]:
    issues = json.loads(path.read_text())["data"]["issues"]
    return [
        {
            "at": i.get("created", "")[:19].replace("T", " "),
            "level": "",
            "source": i["domain"],
            "text": i["issue_id"],
            "kind": "repair",
        }
        for i in issues
    ]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("dir", type=Path)
    p.add_argument("--out", type=Path, default=HERE / "cases" / "history.yaml")
    args = p.parse_args()

    records = read_logs(sorted(args.dir.glob("home-assistant.log*")))
    if (repairs := args.dir / "repairs.issue_registry").exists():
        records += read_repairs(repairs)
    if not records:
        sys.exit(f"nothing in {args.dir}")
    # Saved copies overlap (a log saved before a restart, then .log.1 of the same
    # run): count each record once.
    records = list({(r["at"], r["source"], r["text"]): r for r in records}.values())
    records.sort(key=lambda r: r["at"])  # .log.1 is older than .log

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        groups[signature(r["source"], r["text"])].append(r)

    old = {}
    if args.out.exists():
        old = {c["signature"]: c for c in yaml.safe_load(args.out.read_text()) or []}
    # Earlier runs' signatures stay: HA's log only reaches back to its last
    # start, so history builds up one collect at a time.
    cases = [c for sig, c in old.items() if sig not in groups]
    for sig, rs in sorted(groups.items(), key=lambda g: g[1][0]["at"]):
        first = rs[0]
        prev = old.get(sig, {})
        short = hashlib.sha1(sig.encode()).hexdigest()[:6]
        cases.append(
            {
                "id": prev.get("id", f"h-{first['source'].split('.')[-1]}-{short}"),
                "kind": first.get("kind", "log"),
                "source": first["source"],
                "level": first["level"],
                "text": first["text"][:600],
                "expect": prev.get("expect"),
                "why": prev.get("why", ""),
                "seen": f"{len(rs)}x, {first['at']} to {rs[-1]['at']}",
                "signature": sig,
            }
        )
    args.out.write_text(
        "# Guard's history cases: drafted by collect.py from real HA logs and Repairs.\n"
        "# Label each `expect` System or Informational, with a `why`; null is skipped.\n"
        + yaml.safe_dump(cases, sort_keys=False, allow_unicode=True, width=100)
    )
    labelled = sum(c["expect"] is not None for c in cases)
    print(
        f"{len(records)} records, {len(cases)} signatures ({labelled} labelled) -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
