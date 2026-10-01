"""Jobs from the shell: a file in, a command value out, `/v1` underneath.

The path an operator -- or an agent driving a shell -- needs, and the one that
was missing: a list of 568 addresses is a *file*, and pushing it through a
model's context to submit it is expensive and a transcript risk (measured:
~52KB, and the same list pasted twice). This reads the file, builds the command
value and posts it to `/v1/works`: the same endpoint, the same audit trail and
the same idempotency every other caller uses. No second client library and no
second copy of the rules -- what the API accepts is what this can send.

    submit        --kind K [--source S] (--targets-file F | --targets a,b)
    status        WORK_ID
    units         WORK_ID [--outcome failed] [--limit N]
    retry-failed  WORK_ID

`submit`'s Idempotency-Key is the command value's digest -- the rule the API
itself uses -- so within the API's idempotency window the same file is the same
work rather than a second identical one; after the window, submitting it again is
a new run. Credentials come from the environment (`PEARSS_API_TOKEN`) and are
never printed; the API base defaults to the local deployment and can be moved
with `PEARSS_API_URL`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 30.0


class CliError(Exception):
    """A failure the caller can act on, reported without a traceback."""


def _api_url() -> str:
    return os.environ.get("PEARSS_API_URL", DEFAULT_API_URL).rstrip("/")


def _headers(*, idempotency_key: str | None = None) -> dict[str, str]:
    token = os.environ.get("PEARSS_API_TOKEN", "")
    if not token:
        raise CliError(
            "PEARSS_API_TOKEN is not set. The CLI authenticates the way every "
            "other caller does; point it at the deployment's token (the "
            "justfile sources .env for you)."
        )
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """One call to the Core API, with its errors reported as the API wrote them."""
    url = f"{_api_url()}{path}"
    try:
        response = httpx.request(
            method,
            url,
            headers=_headers(idempotency_key=idempotency_key),
            json=body,
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise CliError(f"could not reach the API at {url}: {exc}") from exc
    if response.status_code >= 400:
        # A refusal from the API has an envelope; a proxy's 502 does not, and
        # both have to become a sentence rather than a JSON decode error.
        try:
            detail = response.json().get("detail", {})
        except ValueError:
            detail = {}
        error = detail.get("error", {}) if isinstance(detail, dict) else {}
        message = error.get("message") or response.text[:200] or "(no message)"
        suggestion = error.get("suggestion", "")
        raise CliError(
            f"{response.status_code} {error.get('code', 'request_failed')}: "
            f"{message}" + (f"\n  {suggestion}" if suggestion else "")
        )
    parsed: dict[str, Any] = response.json()
    return parsed


def _targets(args: argparse.Namespace) -> list[str]:
    """The target list, from a file or a comma-separated argument.

    A file is lines: blanks and `#` comments are skipped, so a list copied out
    of a shell session or a spreadsheet column works as it is.
    """
    if args.targets_file:
        lines = Path(args.targets_file).read_text(encoding="utf-8").splitlines()
        targets = [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
    else:
        targets = [part.strip() for part in (args.targets or "").split(",")]
        targets = [target for target in targets if target]
    if not targets:
        raise CliError("no targets: pass --targets-file <path> or --targets a,b")
    return targets


def _params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.source:
        params["source_id"] = args.source
    if args.batch_size:
        params["batch_size"] = args.batch_size
    if args.min_interval:
        params["min_interval_seconds"] = args.min_interval
    return params


def _command_key(kind: str, targets: list[str], params: dict[str, Any]) -> str:
    """The command value's digest: the same file submits the same work once."""
    from core.work import command_fingerprint

    return command_fingerprint(kind=kind, targets=targets, params=params)


def _submit(kind: str, targets: list[str], params: dict[str, Any]) -> dict[str, Any]:
    return _request(
        "POST",
        "/v1/works",
        body={"kind": kind, "targets": targets, "params": params},
        idempotency_key=_command_key(kind, targets, params),
    )


def cmd_submit(args: argparse.Namespace) -> int:
    """Submit a work; stdout is the handle alone, so it can be captured."""
    targets = _targets(args)
    params = _params(args)
    work = _submit(args.kind, targets, params)
    # One machine-readable line on stdout, the commentary on stderr: a shell
    # script does `WORK=$(just work submit ...)` and must not have to parse it.
    print(work["work_id"])
    print(
        f"submitted {len(targets)} target(s) as kind={args.kind} "
        f"status={work['status']} counts={json.dumps(work['counts'], sort_keys=True)}",
        file=sys.stderr,
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    work = _request("GET", f"/v1/works/{args.work_id}")
    print(f"work_id={work['work_id']}")
    print(f"kind={work['kind']}")
    print(f"status={work['status']}")
    print(f"counts={json.dumps(work['counts'], sort_keys=True)}")
    if work.get("finished_at"):
        print(f"finished_at={work['finished_at']}")
    return 0


def _all_units(work_id: str, outcome: str, limit: int) -> list[dict[str, Any]]:
    """Every page of the ledger, following `next_cursor` to the end."""
    units: list[dict[str, Any]] = []
    cursor = ""
    while True:
        params: dict[str, Any] = {"limit": limit}
        if outcome:
            params["outcome"] = outcome
        if cursor:
            params["cursor"] = cursor
        page = _request("GET", f"/v1/works/{work_id}/units", params=params)
        units.extend(page["data"])
        cursor = page.get("next_cursor") or ""
        if not cursor:
            return units


def cmd_units(args: argparse.Namespace) -> int:
    """One line per target: the ledger as something a shell can consume."""
    for unit in _all_units(args.work_id, args.outcome, args.limit):
        print(
            "\t".join(
                [
                    unit["target"],
                    unit["outcome"],
                    unit["error_class"] or "",
                    unit["detail"] or "",
                ]
            )
        )
    return 0


def cmd_retry_failed(args: argparse.Namespace) -> int:
    """Re-send the failed targets of a work, and only those.

    The whole point of a ledger you can query: "what do I need to re-send?" has
    an answer, and acting on it does not mean re-running the addresses that
    already worked. The new work carries the same kind and params, so it is the
    same command value minus the targets that succeeded.
    """
    work = _request("GET", f"/v1/works/{args.work_id}")
    failed = _all_units(args.work_id, "failed", 100)
    if not failed:
        print("nothing failed: no work to submit", file=sys.stderr)
        return 0
    targets = [unit["target"] for unit in failed]
    params = dict(work.get("params") or {})
    submitted = _submit(work["kind"], targets, params)
    print(submitted["work_id"])
    print(
        f"re-sent {len(targets)} failed target(s) of {args.work_id} "
        f"as kind={work['kind']}",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="just work",
        description="Submit jobs and read their ledgers through /v1.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="Submit a work (a file of targets).")
    submit.add_argument("--kind", required=True, help="fetch, delete, enrich, retry…")
    submit.add_argument("--source", default="", help="source_id the work is about")
    submit.add_argument("--targets-file", default="", help="one target per line")
    submit.add_argument("--targets", default="", help="comma-separated targets")
    submit.add_argument("--batch-size", type=int, default=0)
    submit.add_argument("--min-interval", type=float, default=0.0)
    submit.set_defaults(func=cmd_submit)

    status = sub.add_parser("status", help="A work's status and folded counts.")
    status.add_argument("work_id")
    status.set_defaults(func=cmd_status)

    units = sub.add_parser("units", help="One line per target of a work.")
    units.add_argument("work_id")
    units.add_argument("--outcome", default="", help="ok, skipped, rejected or failed")
    units.add_argument("--limit", type=int, default=100)
    units.set_defaults(func=cmd_units)

    retry = sub.add_parser("retry-failed", help="Re-send only the failed targets.")
    retry.add_argument("work_id")
    retry.set_defaults(func=cmd_retry_failed)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except CliError as exc:
        print(f"work: {exc}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
