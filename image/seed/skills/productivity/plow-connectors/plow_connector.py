#!/usr/bin/env python3
"""Plow connectors helper for Hermes — generic wrapper over the Plow connector
REST API. See `SKILL.md` for the action reference and examples.

`status` is a GET; every other action is a POST whose JSON body is the request.
Auth reuses the gateway's user Bearer token (`PLOW_CONNECTOR_TOKEN` else
`PLOW_AGENT_TOKEN`) against `PLOW_API_BASE` (default https://api.plow.co). A
non-2xx response is fatal: status + body to stderr, non-zero exit.

Pass `--paginate` before the connector to walk a list action's opaque
`meta.next_cursor` to completion and emit one merged `{"status","data":[...]}`.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

GET_ACTIONS = {"status"}
# The only cursor-paged list actions. --paginate on anything else would EXECUTE
# the action (a send is a write) and then mangle its object-valued response.
PAGINATED_ACTIONS = {("gmail", "messages.list"), ("slack", "messages.list")}
MAX_PAGES = 50  # safety cap so an unbounded/looping cursor can't spin forever
# Exactly the actions SKILL.md documents, per connector. An allowlist, not a
# token-shape check: the same bearer token also reaches routes that must never
# flow through a prompted agent's transcript — `access-token` mints a live
# Google OAuth token, `disconnect` destroys state — so anything undocumented
# is rejected here, not at the API.
ACTIONS: dict[str, frozenset[str]] = {
    "gmail": frozenset(
        {
            "status",
            "messages.list",
            "calendar.list",
            "calendar.events.list",
            "calendar.events.create",
            "calendar.events.update",
            "calendar.events.delete",
            "calendar.freebusy",
        }
    ),
    "slack": frozenset(
        {
            "status",
            "channels.list",
            "users.list",
            "conversations.open",
            "messages.list",
            "messages.search",
            "messages.send",
            "messages.update",
            "files.upload",
        }
    ),
}


def _env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _request(connector: str, action: str, payload: dict | None) -> str:
    """Issue one connector request and return the raw response string.

    Shared auth/env/URL plumbing for `call()` and `paginate()`. `payload` is the
    already-parsed POST body (a dict), or None for a GET action.
    """
    allowed = ACTIONS.get(connector)
    if allowed is None:
        raise SystemExit(f"unknown connector {connector!r}; expected one of {', '.join(ACTIONS)}")
    if action not in allowed:
        raise SystemExit(f"unknown action {action!r} for {connector}; see SKILL.md for the documented actions")

    token = _env("PLOW_CONNECTOR_TOKEN", "PLOW_AGENT_TOKEN")
    if not token:
        raise SystemExit("PLOW_CONNECTOR_TOKEN or PLOW_AGENT_TOKEN is required")
    base = _env("PLOW_API_BASE", default="https://api.plow.co").rstrip("/")

    method = "GET" if action in GET_ACTIONS else "POST"
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if method == "POST":
        data = json.dumps(payload or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"

    url = f"{base}/v1/connectors/{connector}/{action}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=60) as resp:
        return resp.read().decode("utf-8")


def call(connector: str, action: str, body: str = "") -> str:
    payload = json.loads(body) if body.strip() else {}
    return _request(connector, action, payload)


def paginate(connector: str, action: str, body: str = "") -> str:
    """Walk the opaque cursor to completion and return one merged response.

    Calls the same path as `call()` repeatedly, echoing `meta.next_cursor` back
    as `body["cursor"]` (omitted on the first call), until the cursor is
    exhausted or `MAX_PAGES` is reached. The cursor is opaque — never parsed.
    Returns `{"status": "ok", "data": [<all pages concatenated>]}`, plus
    `"meta": {"degraded_accounts": [...]}` when any page reported accounts it
    could not read — dropping that would make a partial fan-out look complete.
    """
    if (connector, action) not in PAGINATED_ACTIONS:
        raise SystemExit(f"--paginate only supports {sorted('/'.join(p) for p in PAGINATED_ACTIONS)}, not {connector}/{action}")
    payload = json.loads(body) if body.strip() else {}
    accumulated: list = []
    degraded: list = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page_body = dict(payload)
        if cursor:
            page_body["cursor"] = cursor
        resp = json.loads(_request(connector, action, page_body))
        accumulated.extend(resp["data"])  # list-valued by contract; missing key fails loud
        meta = resp.get("meta") or {}
        degraded.extend(meta.get("degraded_accounts") or [])
        cursor = meta.get("next_cursor")
        if not cursor:
            break
    else:  # loop exhausted without breaking — the cap stopped us before the cursor
        # was exhausted. Fail loud rather than hand back a partial result as success:
        # a --paginate caller asked for the complete set.
        raise SystemExit(
            f"paginate hit MAX_PAGES={MAX_PAGES} cap for {connector}/{action} with the "
            f"cursor still open; refusing to return {len(accumulated)} partial results as success"
        )
    merged: dict = {"status": "ok", "data": accumulated}
    if degraded:
        merged["meta"] = {"degraded_accounts": degraded}
    return json.dumps(merged)


def main(argv: list[str]) -> None:
    paginated = bool(argv) and argv[0] == "--paginate"
    if paginated:
        argv = argv[1:]
    if len(argv) < 2:
        raise SystemExit(__doc__)
    connector, action = argv[0], argv[1]
    body = argv[2] if len(argv) > 2 else ""
    try:
        resp = paginate(connector, action, body) if paginated else call(connector, action, body)
    except urllib.error.HTTPError as exc:  # fail loud — surface the API error verbatim
        detail = exc.read().decode("utf-8", "replace")[:1000]
        sys.stderr.write(f"HTTP {exc.code} {exc.reason}: {detail}\n")
        raise SystemExit(1)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:  # transport / bad body — same fail-loud contract
        sys.stderr.write(f"error: {exc}\n")
        raise SystemExit(1)
    sys.stdout.write(resp if resp.endswith("\n") else resp + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
