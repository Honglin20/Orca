#!/usr/bin/env python3
"""emit_result.py — generic single-line JSON emitter for agent node outputs.

Hard contract: stdout is EXACTLY one line of JSON (the orchestrator parses it);
all logs go to stderr. Values passed via --field are parsed as JSON when they
parse, otherwise kept as strings — so booleans/numbers survive without
callers hand-quoting JSON.
"""
from __future__ import annotations

import argparse
import json
import sys


def _coerce(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Emit a single-line JSON object to stdout.")
    parser.add_argument("--field", action="append", default=[], metavar="K=V",
                        help="set key K to V (V parsed as JSON when possible); repeatable")
    parser.add_argument("--json", dest="inline", default=None, metavar="JSON",
                        help="inline JSON object merged first (--field wins on conflicts)")
    args = parser.parse_args()

    payload: dict = {}
    if args.inline:
        try:
            payload = json.loads(args.inline)
        except json.JSONDecodeError as exc:
            print(f"emit_result: --json is not valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(payload, dict):
            print("emit_result: --json must be a JSON object", file=sys.stderr)
            return 2
    for pair in args.field:
        if "=" not in pair:
            print(f"emit_result: --field expects K=V, got {pair!r}", file=sys.stderr)
            return 2
        key, raw = pair.split("=", 1)
        payload[key] = _coerce(raw)

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
