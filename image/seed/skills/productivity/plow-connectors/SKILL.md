---
name: plow-connectors
description: Use the owner's Plow-connected Google (Gmail + Google Calendar) and Slack accounts. Trigger when the user asks to read/search email, check or create calendar events, check free/busy, or read/search/post Slack messages and list Slack channels/users. Runs through the Plow connector REST API with the gateway's existing Bearer token.
allowed-tools: Bash(python3 /var/lib/hermes/skills/productivity/plow-connectors/plow_connector.py:*)
---

# Plow connectors (Gmail · Google Calendar · Slack)

> **Retiring.** This REST helper is the legacy path. The canonical route for
> Gmail and Google Calendar is the `google-workspace` skill, which reaches
> those accounts through the owner's Mac and handles every account, sending
> included. When this agent has `google-workspace`, use it and leave this
> helper for Slack.

The owner has connected Google and/or Slack to their Plow account. You act on
those accounts through one helper that calls the Plow connector REST API with
the gateway's existing user token — there is nothing to log in to.

```bash
python3 /var/lib/hermes/skills/productivity/plow-connectors/plow_connector.py <connector> <action> '<json>'
```

`<connector>` is `gmail` or `slack`. `status` is the only GET; every other
action takes a JSON body. The helper prints the JSON response and exits
non-zero on an API error (read stderr).

## Pagination

List actions (`messages.list`) return one page plus a `meta.next_cursor` when
more results exist. Pass `--paginate` before the connector to walk that opaque
cursor to completion and emit one merged `{"status":"ok","data":[...]}` with
every page concatenated (`--paginate` accepts only `gmail messages.list` and
`slack messages.list` — anything else is refused before any request is made):

```bash
python3 .../plow_connector.py --paginate gmail messages.list '{"query":"is:unread"}'   # walks all pages
```

The cursor is opaque — the helper echoes it back untouched and stops when the
cursor is exhausted. A 50-page safety cap bounds the walk; hitting it means the
set is larger than expected, so the helper **exits non-zero** rather than return
a partial result as success. When any page reported unreadable accounts, the
merged response carries `meta.degraded_accounts` — check it before treating a
fan-out read as complete. Without `--paginate` you get a single page (pass the
previous response's `meta.next_cursor` as `"cursor"` to page by hand).

## First: check what's connected

```bash
python3 .../plow_connector.py gmail status
python3 .../plow_connector.py slack status    # {"connected":false} means Slack isn't linked yet
```

A user can connect **multiple accounts per connector**, and exactly one of them
is the **default**:

```json
{
  "connected": true,
  "connector": "gmail",
  "account": "me@example.com",
  "accounts": [
    {"account": "me@example.com", "is_default": true, "display_name": "Me"},
    {"account": "side@example.com", "is_default": false, "display_name": null}
  ]
}
```

The top-level `account` field **is the default account** (it always matches the
entry with `is_default: true`). When the user says "my default account", or
names no account for an action that targets one, that is the account to use.

If a connector reports `connected:false`, tell the user it isn't linked to
their Plow account yet — do not attempt actions on it.

### How `account` scopes each action

- **Aggregate reads fan out, they do not use the default.** Omitting `account`
  on `gmail messages.list`, `calendar.list`, `calendar.events.list`,
  `calendar.freebusy`, or `slack messages.search` reads across **all**
  connected accounts/workspaces and merges the results. That is usually right
  for "any unread mail?" or availability checks. Partial failures surface as
  `meta.degraded_accounts` on the Gmail message and Slack search fan-outs
  ONLY. Calendar list/events instead cover the **calendar-scoped** accounts —
  an account connected without Calendar scope is silently absent — and
  freebusy **fails closed** (403) when any connected account lacks Calendar
  scope, rather than reporting a slot free it couldn't check. `gmail
  messages.list` takes no `account` field at all — sending one is rejected
  (422); to read one inbox, filter the merged results by their per-message
  `account`.
- **Calendar writes: the calendar wins, then the default.** A create with
  `calendar_id` omitted (= `"primary"`) and no `account` lands on the
  **default** account's primary calendar. A **named** `calendar_id` resolves
  across ALL accounts and can land on a non-default account's calendar — so on
  `update`/`delete` (and named-calendar creates) always pass BOTH the
  `calendar_id` and the `account` returned by the prior search/create
  response; omitting `account` there is not a default-account guarantee.
- **Slack actions always need the workspace.** Every Slack action except
  `messages.search` requires `account` (the workspace/team id from `status`).
  "My default workspace" → the top-level `account` in `slack status`.

## Gmail (read-only here)

| Action | Body fields |
| --- | --- |
| `messages.list` | `query?`, `after_date?`, `before_date?`, `from_addresses[]?`, `max_results?` (≤25), `cursor?` — no `account`; always reads ALL connected accounts, each result carries its `account` |

```bash
python3 .../plow_connector.py gmail messages.list '{"query":"is:unread","max_results":5}'
```

This helper cannot send, reply to, forward, or fetch a single Gmail message —
the API no longer exposes those routes to it. To act on Gmail beyond listing,
use the `google-workspace` skill (Gmail through the owner's Mac) if this agent
has it; if it doesn't, tell the user sending email isn't available from this
agent — do not improvise another route.

## Google Calendar (under the `gmail` connector)

| Action | Body fields |
| --- | --- |
| `calendar.list` | `account?` (omit → all accounts) |
| `calendar.events.list` | `calendar_id?`, `time_min?`, `time_max?`, `query?`, `max_results?`, `account?` (omit both `calendar_id` and `account` → agenda across all accounts) |
| `calendar.events.create` | `summary`, `start`, `end`, `description?`, `location?`, `attendees[]?`, `calendar_id?`, `time_zone?`, `recurrence[]?`, `add_meet?`, `confirm_conflict?`, `account?` (both omitted → the default account's primary; a named `calendar_id` resolves across accounts) |
| `calendar.events.update` | `event_id`, `calendar_id` (required), `account` (carry BOTH from the prior search/create response), plus any fields to change |
| `calendar.events.delete` | `event_id`, `calendar_id` (required), `account` (carry both from the prior response) |
| `calendar.freebusy` | `time_min`, `time_max`, `time_zone?`, `calendar_ids[]?`, `account?` (omit both → every account's primary/selected calendars) |

Create prechecks conflicts across calendars and returns 409 when the slot is
busy; re-send with `"confirm_conflict": true` to intentionally double-book.

```bash
python3 .../plow_connector.py gmail calendar.events.list '{"time_min":"2026-06-14T00:00:00Z","time_max":"2026-06-21T00:00:00Z","max_results":10}'
```

## Slack

| Action | Body fields |
| --- | --- |
| `channels.list` | `account`, `limit?` |
| `users.list` | `account`, `limit?` |
| `conversations.open` | `account`, `user_id` (returns the DM `channel_id`) |
| `messages.list` | `account`, `channel_id`, `limit?`, `cursor?` |
| `messages.search` | `query`, `limit?`, `account?` (omit → all workspaces) |
| `messages.send` | `account`, `channel_id`, `text`, `thread_ts?` |
| `messages.update` | `account`, `channel_id`, `ts`, `text` |
| `files.upload` | `account`, `channel_id`, `filename`, `data_base64`, `title?`, `thread_ts?`, `initial_comment?` |

```bash
python3 .../plow_connector.py slack channels.list '{"account":"T0123"}'
python3 .../plow_connector.py slack messages.send '{"account":"T0123","channel_id":"C0123","text":"deploy is green"}'
```
