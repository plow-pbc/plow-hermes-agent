# Hermes latch integration — design

Date: 2026-10-31. Status: approved by Sam in chat (playback, 2026-10-31).
Modeled on the OpenClaw latch integration (plow-pbc/plow-openclaw-agent #132,
#133, #134, merged 2026-10-29).

## Why

Hermes agents reach the owner's machines with the latch CLI since
plow-hermes-agent#158 removed the Mac relay, but only the plumbing moved.
Three gaps remain:

1. **Agents are pinned to one Mac.** str sets `LATCH_HOST: mba` agent-wide
   (`str-hermes-agent/compose.yml:64`). When mba sleeps, everything fails,
   including work that another host could do.
2. **The model's latch rules are thin and split across two places.** The base
   `SOUL.md:51` has one sentence about latch. The chat plugin has two latch
   texts:
   - `_plow_facts` (`hermes-plugin-plow/plow-chat-platform/__init__.py:1456-1460`)
     is live on every turn. It covers `latch status`, the `no_host` install
     line, and the rule to retry an offline host on two turns a few minutes
     apart.
   - `LATCH_PROMPT` (`:914`), the relay recall, the relay hints and
     `_backfill_bare_handles` (`:1168-1240`) are gated on `PLOW_MCP_URL`. The
     base no longer publishes that variable, so all of that code is dead.

   Nothing tells the model how to choose a host, to fall back when one is
   asleep, or to name the chat it is answering.
3. **Life still uses the relay.** life-assistant-hermes-agent (main eb7a38e,
   base 7422f99) reaches the Mac through `mcp__plow__plow_*` and `PLOW_MCP_URL`.
   Sam's new Life agent, Cypress, is stuck on calendar setup:
   `life-calendar-discovery` got 503 from the relay 119 times because mba, the
   only relay device, is offline. The `cloud` host could have answered the
   calendar question.

## What Sam decided

- **No agent is tied to one machine.** For each task the agent runs
  `latch status` and uses an online host that can do the job. If its first
  choice is asleep, it tries the next one, and tells the owner only when no
  host can do it. Calendar and Gmail work from any host with `google` on,
  `cloud` included, so "what's on Saturday?" gets an answer while mba sleeps.
- **Messages and contacts exist only on the Mac.** If that host is offline,
  the agent says so and tries again on a later turn. It does not try another
  host. (This matches OpenClaw.)
- **Every latch call made for a chat names that chat** (`--plow-chat`), so the
  gatekeeper judges the request against what was asked there, and an approval
  prompt lands in the right thread.
- **The wiki is str-specific.** Sam: "Wiki is str-specific code. We deprecated
  that from latch." Neither the shared design nor the base knows about a wiki.
  str has its own setting naming the host where its wiki lives (mba for Sam),
  and only str's wiki code reads that setting.
- **Life moves over in the same change.** Life is ported from the relay to the
  latch CLI, rebuilt on the current base, published, and Cypress is updated in
  place, keeping its memory and sessions. **Repointing the `life` catalog
  entry in prod waits for Sam's explicit OK.**

### What "deprecated from latch" means (checked)

- In the old desktop app (plow-pbc/latch, main 81e718d2), the wiki was a
  bundled plugin: `apps/desktop/plugins/wiki/`, a pinned `plow-wiki` binary,
  last bumped in #582 on 2026-10-22.
- latch-cli (plow-pbc/latch-cli, main eb4182cc) replaced that app, and its
  connectors are only Google, Messages and Contacts
  (`latch/src/plugins/mod.rs`). No wiki plugin, no wiki command, and no wiki
  code exist in latch-cli.
- What remains is a folder: `~/Plow/wiki` on one machine, read and written
  with plain `latch file` and `latch exec`, plus the `wiki` program on that
  machine. Owning it is str's job. latch has no wiki concept for this design
  to build on.

## Goals

- One latch contract for every Hermes agent (str, Life, future variants),
  owned in two places: the base for install and environment, and the chat
  plugin for the per-turn instructions the model reads.
- The model's latch rules match OpenClaw's, except where Sam decided
  otherwise. Anything the binary's own `latch skill` already says stays out of
  our prompt.
- Life works when mba is asleep, for everything that doesn't need the Mac.
- Net-negative LOC across the plugin and str: relay-era code gets deleted, not
  wrapped.

## Non-goals

- No change to latch-cli or to the Plow API, unless Sam picks Chunk 4b (see "Open for Sam").
- No wiki in the base or the plugin's shared prompt. (The plugin's existing
  wiki recall stays opt-in, see Chunk 1.)
- No change to OpenClaw.
- No change to customer agents (life-assistant-mary-customer, rowan,
  mark-property). They pick the new image up only through the normal catalog
  path, after Sam's OK, and they are never rolled from this work.
- The stuck Cypress calendar discovery isn't fixed out of band by waking mba.
  That's Sam's option and outside this work.

## Anti-goals

- **Pinning moves elsewhere.** Replacing `LATCH_HOST=mba` with a default host
  that every call still uses is the same pin. Only str's wiki code may name a
  host from configuration.
- **Restating `latch skill`.** The binary's guide already covers the default
  host, naming the host in answers, error codes and command syntax. A second
  copy in our prompt goes stale against the newest latch, which installs at
  every boot.
- **Silent cross-host fallback for Messages and contacts,** or for the
  owner's files when the choice of machine matters. Ask the owner in that
  case, as OpenClaw does.
- **Customer-visible steps** for Cypress (re-setup, re-login, a text the owner
  must send).

## How it fits

### Already in place (base 025fb03), no change

- latch is installed fresh at build and at every boot, by the `latch` oneshot
  before the gateway. An install that fails or takes over 60 s keeps the
  latch already installed. `LATCH_NO_UPDATE_CHECK=1`.
- `LATCH_STATE_DIR=$HERMES_HOME/latch` is on the home volume. The login uses
  the agent's own Plow credential on its first command.
- The `productivity/latch` skill is rewritten from `latch skill` after each
  install.
- The relay's MCP server, `PLOW_MCP_URL` and `HERMES.md` are removed at boot.

OpenClaw also reinstalls before a standby and on an in-place upgrade. Hermes
gets the equivalent from "every boot", because an in-place upgrade is a boot.
Builder: check that, but don't add machinery for it.

### The model's rules (plugin, per turn)

The chat plugin has one latch section. It absorbs the latch paragraph in
`_plow_facts` (`:1456-1460`) and replaces `LATCH_PROMPT`, so a single owner
holds all of the text. It is not gated on `PLOW_MCP_URL`; it is injected
whenever `latch` is on `PATH`. It
carries OpenClaw's rules (`plow-openclaw-agent/prompt/AGENTS.md:107-136`),
rewritten for Sam's decisions:

- Before a task on the owner's machines, read the latch skill. Run
  `latch status` to see the hosts and which plugins each has on. `cloud` is
  the cloud computer.
- Choose the host by what the task needs. Google: any host with `google` on.
  Files and programs: any host that has what's needed. Ask the owner only when
  several could do it and the choice matters (their files, disk, programs).
- If the chosen host is offline, try another host that can do the task. Tell
  the owner only when none can. **Exception: Messages and contacts.** Those
  are the Mac only. If it's offline, say so, and don't try another host.
- With more than one host, pass `--host` on every command.
- Every exec, file, google, messages, contacts, browser and `vault list` call
  passes `--plow-chat <this chat's uid>`. `latch status` doesn't. The plugin
  writes the current chat's uid into the section, so the model never has to
  look it up.
- `no_host`: tell the owner how to install latch on their Mac (OpenClaw's
  text). Offline with no alternative: nothing was sent. Say so, retry next
  turn, and ask the owner to start latch on that machine only after it fails
  on two turns a few minutes apart.

The base `SOUL.md:51` keeps its one sentence. How to use latch belongs to the
plugin section and the skill, not the persona.

### Scripts that act without a chat

Scripts that act with no chat still need a chat for the gatekeeper: str's
nightly and `str-config`, Life's calendar discovery and feed, and the
plugin's wiki recall. All of them act for the home chat.

**Each script's latch helper passes `--plow-chat "$PLOW_HOME_CHANNEL"` on
its own calls.** The base already publishes that variable
(`plow-init.py:1115`).

**No default `LATCH_PLOW_CHAT` goes in the container environment.**
Variables there reach the model's terminal too, through `with-contenv` in
`hermes-gateway/run`. With a default in place, a group-chat turn that forgot
`--plow-chat` would be judged against the owner's home chat. Without the
default, latch refuses the call loudly (`latch-cli latch/src/gate/chat.rs:73`).
A default also breaks `latch vault link-state` (`plow_chat_not_supported`,
`help.rs:1711`).

str's `bin/latch-boot` publishes that default today, and it is deleted
(Chunk 3). The plugin's `_latch_file` copy (`__init__.py:3910`) becomes an
explicit flag.

### str's wiki host

- `compose.yml` drops `LATCH_HOST: mba` and sets `PLOW_WIKI_HOST: mba`.
- `bin/plow_relay.py` serves two callers. The wiki path uses it
  (`ingest-all`, `nightly.sh`). So does `str-config` (`:20`, `:170-191`),
  which reads and writes the owner's key file, `~/Plow/str-keys.env`.
  - **My call:** str's owner-side files live with its wiki, so both use
    `PLOW_WIKI_HOST`. The key file is in one place, like the wiki, and
    setup puts it there.
  - `plow_relay.py` passes `--host "$PLOW_WIKI_HOST"` and
    `--plow-chat "$PLOW_HOME_CHANNEL"` on every call it makes.
  - `setup-discover`'s latch check asks whether that host is online.
  - str's model-driven latch calls (guest work, Google) follow the shared
    rules and pick any host.
- The plugin's wiki recall (`_latch_file`, used only when
  `PLOW_WIKI_EMBED_URL` is set, and only str sets it) passes
  `--host "$PLOW_WIKI_HOST"` when the variable is set.
- If the wiki host is offline, wiki reads and writes fail with
  `host_offline`, exactly as today. No other host is tried, because the wiki
  lives in one place.

The variable name is my call. It sits beside the plugin's existing
`PLOW_WIKI_EMBED_URL` and `WIKI_WRITER`. The plugin's wiki code stays in the
plugin, dormant without `PLOW_WIKI_EMBED_URL`. Moving recall into str is out
of scope; Sam's "str-specific" is satisfied as long as nothing shared depends
on it.

### Life's port

Every `mcp__plow__plow_*` call and every `PLOW_MCP_URL` read in
life-assistant-hermes-agent moves to the latch CLI. About 30 files carry
these calls (counted on eb7a38e): s6 runs, scripts, skills, tests and the
README. The mapping:

| Relay | latch |
|---|---|
| `plow_run_command(["plow-gog", "calendar", …])` on the Mac | `latch google --host <any host with google> -- calendar …`. No Mac needed. The fan-out envelope replaces the per-account loop where one exists. |
| `plow_run_command` for gog Gmail reads (morning triage) | `latch google … -- gmail search …` |
| `plow_run_command` reading Messages (morning triage) | `latch messages … --host <the Mac>`, Mac only, per the rule above |
| `plow_write_file` / `plow_read_file` of `~/Plow/ld/*` | `latch file` on the host that holds them (below) |
| `plow_run_command(["sh","-c","curl … <pi>"])` (kiosk delivery, wall setup) | `latch exec --network` on a host on the owner's LAN. That's the Mac in practice; when it's offline, say so. |
| `latch_status.py` (`PLOW_MCP_URL` set?) | `latch status`: configured means logged in, with at least one online host |
| `relay(url, token, …)` in `calendar_feed.py` / `calendar_discovery.py` | `subprocess` to `latch`, parsing latch's one-JSON-object output and `{"error": {code, …}}`, as str's `plow_relay.py` does |

Calendar discovery and the calendar feed are the cases that unblock Cypress:
they need only Google, so they must work with mba asleep.

Two decisions are left to the builder:

- **Where `~/Plow/ld/` lives.** It's on the Mac today. Only the kiosk-delivery
  path needs it on a LAN host. If the feed can write `calendar.json` on
  whichever host posts to the kiosk, decide that, and keep a single owner for
  the folder's location.
- **How each script picks a host.** It reads `latch status` once per run and
  takes the first online host with `google` on. Don't add a configured
  default host (see Anti-goals).

**Kiosk delivery and the gatekeeper (my call, verify first).** Life's
unattended delivery relies on a relay-era rule that anything under `~/Plow`
auto-approves on the Mac (`calendar_feed.py:86`, `latch-delivery.md:17`).
latch has no such rule. Its gatekeeper (`latch-cli latch/src/gate/mod.rs:5-14`)
passes everyday reads and file edits without asking, but an unattended
`latch exec --network` curl to the Pi may need the owner's OK.

On the throwaway, the builder checks which delivery calls go through
unasked. If any call needs an OK:
- Delivery from that host stands down with a logged reason.
- An issue is filed.
- No owner step and no host rule is added in this work.

Calendar discovery and the feed's Google reads are pure Google and aren't
affected.

### Rollout and the Cypress update

The order is plugin, then base pin, then str and Life. Plugin → base pins are
pointer-only and carry standing authorization. str rolls through its own
deploy skill and smoke. Life:

1. Merge the port, pinned to the newest base (025fb03 or the chunk-2 base,
   whichever is newer at the time).
2. Publish the Life image.
3. Update Cypress, keeping its state (memory, sessions,
   `ld/calendar-discovery.json`). Cypress is Sam's own Plow cloud agent, uid
   `4bf0b5dda19fb707308c8e3c6b82950b`. **Open: there is no in-place path for
   a Hermes agent today.**
   - Plow's in-place upgrade (`POST /v1/agents/{uid}/upgrade`, plow #2474,
     `api/plow/assistant/exe.py`) keeps `/var` and swaps the image. It
     refuses any image without the OpenClaw layout (`usr` + `opt/plow` +
     `app`, `exe.py:1139-1141`). Hermes images have no `/app`.
   - The upgrade defaults to the catalog pin. Naming a different digest is
     admin-only.
   - See "Open for Sam". Whichever path, rehearse it on a throwaway first,
     then verify in Cypress's own runtime that calendar discovery leaves
     `pending` with mba still asleep.
4. **Stop.** `PUT /v1/agent-images/life` (prod catalog) needs Sam's explicit
   OK. Customer Life agents (Mary, Rowan) are never touched by this work.

## Open for Sam

**How Cypress gets the new Life image.**
- **Recommended:** a small Plow PR teaching the in-place upgrade the Hermes
  layout. `opt/plow` + `opt/hermes`, state under `/var/lib/hermes`, which
  the upgrade already leaves alone. Every Hermes cloud agent needs this to
  reach a base fix without losing its work, Mary and Rowan included, later
  and only with Sam's OK. This adds Chunk 4b and lifts the "no Plow change"
  non-goal for that one route.
- **Alternative:** Cypress was installed today, so re-provisioning it costs
  little: an hour of onboarding and a fresh setup. It is still a step Sam
  performs.

Either way, the prod catalog repoint stays Sam's call.

## Constraints

- No git worktrees. Each repo is a feature branch and a PR. Pin PRs are
  pointer-only.
- Never print secrets. Message bodies appear only as counts and metadata.
- No texting anyone as part of verification. Handset checks are Sam's.
- Customer agents are inspect-only.
- Life's prod catalog repoint is gated on Sam.

## Chunks

Each chunk is one PR unless noted. Dependencies are noted per chunk.

### Chunk 1: plugin latch section (hermes-plugin-plow)

- Replace `LATCH_PROMPT` and its `PLOW_MCP_URL` gate with the latch-CLI
  section described in "The model's rules". Inject it whenever `latch` is on
  `PATH`, and write the current chat's uid into it.
- Fold `_plow_facts`' latch paragraph (`:1456-1460`) into the section.
- Delete the dead relay code gated on `PLOW_MCP_URL`: relay recall, relay
  hints, `_mac_skills_section`, and the reply-framing clause near `:5730`.
  Delete or update their tests.
- Delete `_backfill_bare_handles` (`:1168-1240`), which already does nothing
  on a latch-only agent, so nothing regresses. File an issue for porting it
  to `latch contacts` (Mac only).
- Align `seed-skills/productivity/owners-mac/SKILL.md` with the section
  (Mac-only for Messages and contacts, `--plow-chat`) without restating
  `latch skill`.
- In `_latch_file`, replace the `LATCH_PLOW_CHAT` copy with an explicit
  `--plow-chat "$PLOW_HOME_CHANNEL"`. Its wiki recall passes
  `--host "$PLOW_WIKI_HOST"` when the variable is set.
- **Done when:**
  - A turn's system prompt carries the section, with that chat's uid, on an
    agent with no `PLOW_MCP_URL`.
  - `grep PLOW_MCP_URL` on the plugin returns nothing.
  - The plugin's net LOC is negative.
  - `just test` is green.

### Chunk 2: base pin (plow-hermes-agent). Depends on Chunk 1.

- A pointer-only pin of the Chunk 1 plugin. The base gets no code change.
- **Done when:**
  - A `hermes chat` turn's prompt carries the latch section.
  - The image is published.

### Chunk 3: str (str-hermes-agent). Depends on Chunk 2.

- `compose.yml`: drop `LATCH_HOST`, add `PLOW_WIKI_HOST: mba`.
- `plow_relay.py`, which serves both `str-config` and the wiki, passes
  `--host "$PLOW_WIKI_HOST"` and `--plow-chat "$PLOW_HOME_CHANNEL"`.
  `setup-discover` checks that host.
- Delete `bin/latch-boot` and the `str-latch` s6 oneshot. The base owns relay
  removal (since #158), latch's first-use login covers logging in
  (`latch-cli latch/src/main.rs:248`), and the default chat is gone on
  purpose (see "Scripts that act without a chat").
- `runtime/config.yaml:94-96` disables the plugin's `owners-mac` and
  `google-workspace` skills because they "route through the relay". That is
  no longer true, so drop the disable. Check before deleting: if str's explicit login covers something
  first-use login doesn't, such as a nightly that runs before any command,
  record that in the PR.
- Update README § latch (around `:1595`-`:1615`) and the tests.
- Pin the Chunk 2 base. Deploy with `deploy-str-hermes` and smoke it.
- **Done when:**
  - In the running container, `LATCH_HOST` is unset and `PLOW_WIKI_HOST=mba`.
  - With mba offline, a str turn asking for a Google read answers from another
    host, verified on the tool log, not the reply text.
  - A wiki read fails `host_offline` naming mba.
  - Smoke steps 1–4 pass. Step 5, the handset check, is Sam's.

### Chunk 4: Life port (life-assistant-hermes-agent). Depends on Chunk 2, the base with the latch section; can be built in parallel with Chunk 3.

- Port every relay call per the mapping table. Delete `PLOW_MCP_URL` reads
  from the s6 runs and scripts, and rewrite the skills' tool instructions to
  latch commands. A Life-local `latch` helper replaces `relay()`; reuse str's
  `plow_relay.py` shape, but don't share the code across repos.
- Pin the newest base.
- **Done when:**
  - `grep -E 'mcp__plow__|plow_run_command|PLOW_MCP_URL'` on the repo returns
    nothing.
  - `just test` is green.
  - On a throwaway container from the built image, with the operator's own
    latch account and mba offline, calendar discovery completes from a
    non-Mac host.

### Chunk 4b, only if Sam picks the recommended path: Plow in-place upgrade for Hermes (plow)

- `exe.py`'s rootfs and tree checks accept the Hermes layout, with no
  OpenClaw-only assumptions in the swap.
- **Done when:** a throwaway Hermes cloud agent is upgraded to a new digest
  in place, keeps its `/var/lib/hermes`, and comes back healthy.

### Chunk 5: publish and update Cypress (ops, no PR). Depends on Chunk 4, and on 4b or Sam's re-provision OK.

- Publish the Life image.
- Rehearse the chosen path on a throwaway, then apply it to Cypress.
- Verify in Cypress's runtime: `ld/calendar-discovery.json` leaves `pending`,
  and the discovery log shows `latch google` calls with no 503s.
- Report, then **stop** before `PUT /v1/agent-images/life`, which needs Sam's
  OK.
- **Done when:** Cypress's calendar discovery has completed with mba asleep,
  and the catalog repoint has been handed to Sam with the exact command.

## Left to the builder

- Exact prompt wording, within the rules above and OpenClaw's text as the
  starting point.
- Where `~/Plow/ld/` lives, and how the kiosk-delivery host is chosen (see
  Life's port).
- How the plugin detects latch (`shutil.which`, or the base's install path).
- Whether any relay-era test fixtures are worth converting rather than
  deleting.

## Calls I made (check these while reading)

- **Settings and defaults**
  - `PLOW_WIKI_HOST` is the name of str's wiki-host setting. str's key file
    uses it too.
  - Each script passes `--plow-chat` explicitly, rather than reading a
    fleet-wide `LATCH_PLOW_CHAT` default.
- **Plugin cleanup**
  - The plugin's two latch texts merge into one section.
  - `_backfill_bare_handles` is deleted, and its port is filed as an issue.
- **str**
  - str re-enables the plugin's `owners-mac` and `google-workspace` skills.
- **Life**
  - If kiosk delivery needs an owner's OK under latch, it stands down and an
    issue is filed. No owner step is added.
