#!/usr/bin/env python3
"""Read and triage the feature backlog.

The backlog fills itself from chat: an explicit "can you add X", and — more
usefully — every question Nano could not answer, captured when it happened.
This is the other half, where a human decides what to do about it.

    python scripts/backlog.py                       # what's been asked for
    python scripts/backlog.py --candidates          # recurring unserved questions
    python scripts/backlog.py --plan <id>           # mark as planned
    python scripts/backlog.py --done <id>           # mark as done
    python scripts/backlog.py --decline <id>        # mark as declined
    python scripts/backlog.py --issue <id>          # open a GitHub issue for it

Export is deliberately manual. These are free-text messages from a private
WhatsApp chat and the repository is going public, so a person decides what
becomes an issue — nothing is pushed automatically.

Needs GOOGLE_CLOUD_PROJECT, BQ_DATASET and ADC. `--issue` also needs `gh`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from nano import backlog, question_log
from nano.config import get_settings
from nano.logging_setup import configure_logging


def show_backlog(settings) -> int:
    entries = backlog.open_requests(limit=50, settings=settings)
    if not entries:
        print("Backlog is empty — nothing has been asked for that Nano couldn't do.")
        return 0

    print(f"{len(entries)} open request(s), most-wanted first:\n")
    for entry in entries:
        count = entry.get("request_count") or 1
        times = "once" if count == 1 else f"{count} times"
        issue = entry.get("github_issue_url") or ""
        print(f"  {entry['request_id']}")
        print(f"    {textwrap.shorten(entry.get('text', ''), 90)}")
        print(
            f"    asked {times} · {entry.get('source')} · {entry.get('status')}"
            f" · {entry.get('days_open', 0)}d open" + (f" · {issue}" if issue else "")
        )
        print()
    return 0


def show_candidates(settings) -> int:
    """Questions asked repeatedly that no template serves.

    Different from the backlog: these are questions Nano *can* answer, but only
    by writing SQL each time. A recurring one has earned a hand-written query.
    """
    rows = question_log.template_candidates(limit=20, settings=settings)
    if not rows:
        print("No template candidates yet — no question has recurred unserved.")
        return 0

    print(f"{len(rows)} question(s) that recur and no template covers:\n")
    for row in rows:
        print(f"  asked {row['times_asked']}x · {row['unanswered']} unanswered")
        print(f"    {textwrap.shorten(row.get('example_question', ''), 90)}")
        print()
    return 0


def open_issue(request_id: str, settings) -> int:
    entries = [
        e
        for e in backlog.open_requests(limit=200, settings=settings)
        if e["request_id"] == request_id
    ]
    if not entries:
        print(f"No open request with id {request_id}", file=sys.stderr)
        return 1
    entry = entries[0]

    if entry.get("github_issue_url"):
        print(f"Already has an issue: {entry['github_issue_url']}")
        return 0

    count = entry.get("request_count") or 1
    body = textwrap.dedent(
        f"""\
        Requested through Nano's chat.

        > {entry.get("text", "")}

        - Asked: {count} time(s)
        - Source: {entry.get("source")}
        - First asked: {entry.get("first_requested_at")}
        - Backlog id: `{request_id}`
        """
    )

    print("About to open a GitHub issue with this body:\n")
    print(textwrap.indent(body, "    "))
    if input("Open it? [y/N] ").strip().lower() not in {"y", "yes"}:
        print("Cancelled.")
        return 0

    title = textwrap.shorten(entry.get("text", "Feature request"), 70)
    result = subprocess.run(
        ["gh", "issue", "create", "--title", title, "--body", body, "--label", "enhancement"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"gh failed: {result.stderr.strip()}", file=sys.stderr)
        return 1

    url = result.stdout.strip().splitlines()[-1]
    backlog.set_status(request_id, backlog.PLANNED, github_issue_url=url, settings=settings)
    print(f"Opened {url} and marked the request as planned.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", action="store_true", help="recurring unserved questions")
    parser.add_argument("--plan", metavar="ID")
    parser.add_argument("--done", metavar="ID")
    parser.add_argument("--decline", metavar="ID")
    parser.add_argument("--issue", metavar="ID", help="open a GitHub issue for this request")
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()

    if args.candidates:
        return show_candidates(settings)
    if args.issue:
        return open_issue(args.issue, settings)

    for flag, status in (
        (args.plan, backlog.PLANNED),
        (args.done, backlog.DONE),
        (args.decline, backlog.DECLINED),
    ):
        if flag:
            ok = backlog.set_status(flag, status, settings=settings)
            print(f"{flag} -> {status}" if ok else f"failed to update {flag}")
            return 0 if ok else 1

    return show_backlog(settings)


if __name__ == "__main__":
    raise SystemExit(main())
