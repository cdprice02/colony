# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml==6.0.2"]
# ///
"""Guard's test runner.

Sends each case through Guard's prompt to Ollama exactly as Home Assistant's AI Task
would (Home Assistant 2026.10.1: the AI Task system prompt plus the date line,
the rendered prompt as the user message, the structure as a JSON schema, think
off, keep_alive -1, num_ctx from the AI Task subentry) and scores the answers.

Pass bar: zero misses on must-push cases (expected System), and at most 1 false
push in 10 Informational cases. A case with no verdict (timeout, bad JSON) counts
as System, as Guard would treat it, unpushed: a miss if it was must-push.

The pre-filter (filters.yaml beside the prompt) is applied first, as the
automation will: a matching case is Informational without asking the model. A
filtered must-push case is a miss; filtered Informational cases leave the bar's
denominator, so the filter cannot buy a pass.

Usage:
  uv run guard/run.py [cases.yaml ...] [--model tev1:4b] [--repeat 3]

Environment:
  GUARD_PROMPT   the prompt file   prompt.yaml beside this script
  GUARD_FILTERS  the pre-filter    filters.yaml beside the prompt
  OLLAMA_URL     the model host    http://localhost:11434
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TIERS = ["System", "Informational"]
VARS = ("kind", "source", "level", "text")
# ai_task DEFAULT_SYSTEM_PROMPT and llm.DATE_TIME_PROMPT, joined as chat_log does.
SYSTEM = "You are a Home Assistant expert and help users with their tasks.\n{date}"
SCHEMA = {
    "type": "object",
    "properties": {"tier": {"type": "string", "enum": TIERS}},
    "required": ["tier"],
}


def load_prompt(path: Path) -> tuple[str, str]:
    raw = path.read_text()
    version = re.search(r"guard-prompt (v\d+)", raw)
    template = yaml.safe_load(raw)
    if not isinstance(template, str):
        sys.exit(f"{path}: expected one block scalar")
    return template, version.group(1) if version else "unversioned"


def render(template: str, case: dict) -> str:
    out = re.sub(
        r"\{\{\s*(\w+)\s*\}\}",
        lambda m: str(case.get(m.group(1)) or "") if m.group(1) in VARS else m.group(0),
        template,
    )
    if "{{" in out or "{%" in out:
        sys.exit(f"prompt uses more than {{{{ {' | '.join(VARS)} }}}}: keep it plain")
    return out


def load_filters(path: Path) -> list[dict]:
    rules = yaml.safe_load(path.read_text()) or []
    for rule in rules:
        if not {"kind", "source", "text"} & rule.keys() or not rule.get("why"):
            sys.exit(f"{path}: every rule needs a why and kind, source or text: {rule}")
    return rules


def filtered(case: dict, rules: list[dict]) -> dict | None:
    for rule in rules:
        if (
            rule.get("kind", case["kind"]) == case["kind"]
            and re.search(rule.get("source", ""), case["source"])
            and re.search(rule.get("text", ""), str(case["text"]))
        ):
            return rule
    return None


def ask(url: str, model: str, options: dict, prompt: str, timeout: float) -> dict:
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM.format(
                    date=datetime.now().strftime(
                        "Current time is %H:%M:%S. Today's date is %Y-%m-%d.\n"
                    )
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "keep_alive": -1,
        "options": options,
        "think": False,
        "format": SCHEMA,
    }
    req = urllib.request.Request(
        f"{url}/api/chat",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.load(resp)
    except (urllib.error.URLError, TimeoutError) as err:
        return {"tier": None, "error": str(err), "ms": None, "tokens": None}
    ms = round((time.monotonic() - start) * 1000)
    tokens = reply.get("prompt_eval_count")
    try:
        tier = json.loads(reply["message"]["content"])["tier"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return {"tier": None, "error": "bad output", "ms": ms, "tokens": tokens}
    if tier not in TIERS:
        return {"tier": None, "error": f"bad tier {tier!r}", "ms": ms, "tokens": tokens}
    return {"tier": tier, "error": None, "ms": ms, "tokens": tokens}


def load_cases(paths: list[Path]) -> list[dict]:
    cases = []
    for path in paths:
        for case in yaml.safe_load(path.read_text()) or []:
            if case.get("expect") not in TIERS:
                continue  # unlabelled (a fresh collect.py draft)
            case["file"] = path.name
            cases.append(case)
    ids = [c["id"] for c in cases]
    if dupes := {i for i in ids if ids.count(i) > 1}:
        sys.exit(f"duplicate case ids: {sorted(dupes)}")
    return cases


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("cases", nargs="*", type=Path)
    p.add_argument("--model", default="tev1:4b")
    p.add_argument(
        "--num-ctx",
        type=int,
        default=2050,
        help="the AI Task subentry's context window (tev1's own is 2050)",
    )
    p.add_argument(
        "--temperature",
        type=float,
        help="stand-in for a Modelfile variant; HA itself never sends one",
    )
    p.add_argument("--repeat", type=int, default=1, help="ask each case N times")
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--only", help="regex over case ids")
    p.add_argument(
        "--save", action="store_true", help=f"write {HERE.name}/results/*.jsonl"
    )
    args = p.parse_args()

    prompt_path = Path(os.environ.get("GUARD_PROMPT", HERE / "prompt.yaml")).expanduser()
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    template, version = load_prompt(prompt_path)
    filters_path = Path(
        os.environ.get("GUARD_FILTERS", prompt_path.parent / "filters.yaml")
    ).expanduser()
    rules = load_filters(filters_path)
    cases = load_cases(args.cases or sorted((HERE / "cases").glob("*.yaml")))
    if args.only:
        cases = [c for c in cases if re.search(args.only, c["id"])]
    if not cases:
        sys.exit("no labelled cases")

    options: dict = {"num_ctx": args.num_ctx}
    if args.temperature is not None:
        options["temperature"] = args.temperature
    results = []
    for case in cases:
        if rule := filtered(case, rules):
            ok = case["expect"] == "Informational"
            print(
                f"{'filt' if ok else 'MISS'} {case['id']:<34} expect "
                f"{case['expect']:<13} filtered: {rule['why']}"
            )
            results.append(
                {
                    "id": case["id"],
                    "file": case["file"],
                    "expect": case["expect"],
                    "got": ["Informational"],
                    "filtered": rule["why"],
                    "errors": [],
                    "ms": [],
                    "tokens": 0,
                }
            )
            continue
        prompt = render(template, case)
        answers = [
            ask(url, args.model, options, prompt, args.timeout)
            for _ in range(args.repeat)
        ]
        for a in answers:
            if a["tokens"] and a["tokens"] >= args.num_ctx:
                sys.exit(f"{case['id']}: prompt fills the {args.num_ctx}-token context")
        # Guard's rule: no verdict means System. Any answer short of the expected
        # tier in any repeat fails the case, so flaky answers show as failures.
        got = [a["tier"] or "System" for a in answers]
        wrong = [g for g in got if g != case["expect"]]
        mark = (
            "ok  " if not wrong else ("MISS" if case["expect"] == "System" else "PUSH")
        )
        errors = sorted({a["error"] for a in answers if a["error"]})
        ms = sorted(a["ms"] for a in answers if a["ms"] is not None)
        print(
            f"{mark} {case['id']:<34} expect {case['expect']:<13} got {'/'.join(got)}"
            + (f"  [{'; '.join(errors)}]" if errors else "")
        )
        results.append(
            {
                "id": case["id"],
                "file": case["file"],
                "expect": case["expect"],
                "got": got,
                "errors": errors,
                "ms": ms,
                "tokens": max((a["tokens"] or 0) for a in answers),
            }
        )

    must = [r for r in results if r["expect"] == "System"]
    info = [
        r for r in results if r["expect"] == "Informational" and "filtered" not in r
    ]
    n_filtered = sum("filtered" in r for r in results)
    misses = [r for r in must if any(g != "System" for g in r["got"])]
    pushes = [r for r in info if any(g == "System" for g in r["got"])]
    all_ms = sorted(m for r in results for m in r["ms"])
    passed = not misses and len(pushes) * 10 <= len(info)
    print(
        f"\nmodel {args.model}, prompt {version}, options {options}, repeat {args.repeat}"
    )
    print(
        f"must-push: {len(must) - len(misses)}/{len(must)} pushed, {len(misses)} missed"
    )
    print(
        f"informational: {len(pushes)}/{len(info)} falsely pushed (bar: at most 1 in 10)"
    )
    if all_ms:
        print(
            f"latency: median {all_ms[len(all_ms) // 2]} ms, max {all_ms[-1]} ms; "
            f"largest prompt {max(r['tokens'] for r in results)} tokens"
        )
    print(f"filtered before the model: {n_filtered} ({filters_path.name})")
    print("PASS" if passed else "FAIL")

    if args.save:
        out = (
            HERE
            / "results"
            / f"{datetime.now():%Y-%m-%dT%H%M}-{args.model.replace(':', '-')}-{version}.jsonl"
        )
        out.parent.mkdir(exist_ok=True)
        out.write_text(
            "".join(
                json.dumps(
                    {
                        "model": args.model,
                        "prompt": version,
                        "options": options,
                        **r,
                    }
                )
                + "\n"
                for r in results
            )
        )
        print(f"saved {out.relative_to(HERE.parent.parent)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
