# plow-hermes-agent

The base OCI image for a hosted Plow agent.

A Plow agent is a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
supervised by [s6-overlay](https://github.com/just-containers/s6-overlay),
reachable only through Plow Chat. One image serves two paths: exe.dev unpacks it
into a VM rootfs and boots its `Cmd`, and `docker run` boots the same `Cmd` in a
container — `/init` either way, so what the developer runs is what the tenant
gets. The image is credential-free and tenant-free — the host sets
`PLOW_API_BASE` (and optionally `AGENT_ID`, and `PLOW_AGENT_TOKEN` where no
proxy injects it) in the container's environment and the image does the rest. It adds one package
to the runtime's environment (`pydantic-settings`, pinned, `--no-deps`) and no
code of its own beyond the init below.
There is no local mode: a developer's machine sets the same variables and gets
the same boot, which is what makes the one path worth checking.

## The repos

One Plow agent is assembled from these repos. Before you change something,
find the row that owns it. If the row is not the repo you are in, the change
goes there; the repos that consume it follow, by bumping their pins if they hold one. The test: **who else would have to
change if this fact changed?** One owner, one place.

| repo | owns | not here |
| --- | --- | --- |
| [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) | the runtime: gateway, tool schema, sessions, MCP client | anything Plow-shaped |
| [`srosro/hermes-agent`](https://github.com/srosro/hermes-agent) | staging for changes going upstream — upstream-fit only; a generic fix or feature Hermes itself would take | anything only Plow needs; that is the plugin or the base |
| this repo | the base image: boot, `plow-init`, the gateway config seed, the base persona, the plugin pin | a persona, a skill for one agent, a Plow tool, the per-turn prompt framing |
| [`plow-pbc/hermes-plugin-plow`](https://github.com/plow-pbc/hermes-plugin-plow) | the `plow_chat` plugin: how every turn is framed, the Plow tools, the three seed skills | chat data (plow), boot and config (base), grammar Latch already owns |
| a variant, e.g. [`plow-pbc/life-assistant-hermes-agent`](https://github.com/plow-pbc/life-assistant-hermes-agent) | one assistant: its persona, its skills, its defaults | gateway config, trust policy, mount paths, clients for Plow or Latch, anything a second assistant would want |
| [`plow-pbc/plow`](https://github.com/plow-pbc/plow) (private) | the API, the relay, the dashboard, and the registry `api/cloud-agents/agents.json` that pins which image tenants boot | anything about the inside of an image; any branch on which assistant this is |
| [`plow-pbc/plow-agents`](https://github.com/plow-pbc/plow-agents) | minting, rotating and retiring the credential that runs any of these images on a machine of your own | the compose file each image repo ships; an agent's persona or skills; a second copy of a plow CLI command |
| [`plow-pbc/latch`](https://github.com/plow-pbc/latch) | the Mac side: the MCP tools, what they say about themselves, the gog grammar | the relay; that is plow |

[`plow-pbc/agent-mgr`](https://github.com/plow-pbc/agent-mgr) is the
deprecated Docker fleet runner; it still pins the plugin and seed skills by SHA
until `plow-agents` can run a container.

Two habits keep this map true. A variant that needs something from the base
opens a PR on the base, then bumps its digest; it does not carry the fix
itself. A Hermes bug goes to the fork and upstream; the base has no patch
mechanism on purpose, so until upstream lands the plugin carries the
workaround.

**Not here:**

- The per-turn prompt framing and the Plow tool descriptions — owned by the
  `plow_chat` plugin in `hermes-plugin-plow`; this repo carries only its pin. The
  base persona in `image/seed/SOUL.md` is this repo's.
- A persona or a skill for one assistant — owned by that assistant's variant
  repo, which builds `FROM` this image.
- A patch to the Hermes runtime — owned by `srosro/hermes-agent` and then
  upstream; the base has no patch mechanism on purpose.

**Examples:**

- Adheres — #31 deleted this repo's tracked copies of both seed skills (−156
  lines) and staged them from the plugin tarball instead, so skill text and the
  plugin it describes come from one commit and move together on a pin bump:
  https://github.com/plow-pbc/plow-hermes-agent/pull/31
- Violates — #21 (and its duplicate #22) applied to this repo's tracked copy of
  `plow-invite/SKILL.md` the identical edit `hermes-plugin-plow` had already made
  in the canonical copy: two PRs for one text change, in the repo that does not
  own the text: https://github.com/plow-pbc/plow-hermes-agent/pull/21

`docker build` produces the architecture you are on; the tags published so far
carry `amd64` alone.

## What is in the image

| path | what it is |
|---|---|
| `/var/lib/hermes/` | the agent's home (`HERMES_HOME` and `HERMES_WRITE_SAFE_ROOT`, set as image ENV so everything in the image agrees on it), `3770 root:hermes` — `config.yaml` (overrides only, every tenant value a `${...}` reference), `SOUL.md` (the identity, root-owned, composed at boot), `skills/` |
| `/opt/hermes/plugins/plow_chat/` | the chat plugin, bundled rather than seeded into the home, so the agent's phone line does not live in a directory the agent can write |
| `/opt/hermes/skills/` | the same seed skills again, out of the agent's reach; the gateway seeds them into a home that lacks them and updates the ones the agent has not customised. A skill the agent deleted stays deleted — the runtime records that and honours it |
| `/etc/s6-overlay/scripts/plow-init.py` | the oneshot: repairs the home's ownership, reads `PLOW_API_BASE`, asks Plow who this agent is, publishes the answer, and edits the config as the agent |
| `/etc/cont-init.d/00-plow-sanitize` | seeds `config.yaml` if the home has none |
| `/etc/s6-overlay/s6-rc.d/hermes-gateway/` | longrun: the gateway as `hermes`, depending on `plow-init` |
| `/etc/s6-overlay/s6-rc.d/home-guard/` | longrun, as root, depending on `plow-init`: every 10s puts `/var/lib/hermes` and `skills/` back to `3770 root:hermes`, and logs what it found whenever something else changed them |

## The environment, and the bearer

Provisioning's whole involvement with a tenant's VM is its environment. exe.dev
writes it to `/exe.dev/etc/env` and hands it to the image's CMD; s6-overlay
imports it into `/run/s6/container_environment`, and `plow-init` runs under
`with-contenv`, so it reads the values straight from its process environment:

```
PLOW_API_BASE=https://plow-<agent_uid>.int.exe.xyz
AGENT_ID=life # optional: the registered Agent Index id
```

`PLOW_API_BASE` is an exe.dev **integration**: an HTTP proxy in front of Plow
that replaces the `Authorization` header of every request with the agent's own
credential. The image never sees that credential, so there is none to leak,
rotate or persist inside the VM. Where a bearer has to be present anyway — the
`plow_chat` plugin requires `PLOW_AGENT_TOKEN`, and the inference provider names
its key by variable (`HERMES_CUSTOM_PLOW_API_KEY`) — `plow-init` publishes the
fixed placeholder `proxied`, which the proxy overwrites and Plow never sees.
A host with no such proxy — a developer's compose — sets `PLOW_AGENT_TOKEN`
beside `PLOW_API_BASE`, and that token is used everywhere instead; the
placeholder only ever fills an absent token, never replaces a real one.
`AGENT_ID` is the provisioner's, set from the selected cloud variant; the
image passes it through untouched for a variant's index reporter.

With that, `plow-init` asks Plow who this agent is:
`GET $PLOW_API_BASE/v1/agents/cloud/me` answers with this agent's line, the
chats it is in, and a relay endpoint. Plow does not name a home channel, so the
image picks one: the active chat holding exactly this agent and exactly one
member, who is the owner. Zero matches waits for first contact, polling every
30 seconds until a home chat appears. Several matches still park, printing the
roster it saw — the wrong home is an agent talking to the wrong people. From that, the image publishes the
tenant's environment
itself — one file per name under `/run/s6/container_environment`, which every
service inherits — adding the placeholder bearer and generating a fresh
`API_SERVER_KEY` on every boot.

The loopback `API_SERVER_KEY` is the one value written to disk, and only
because it has to be:
the runtime writes a key of its own into `$HERMES_HOME/.env` during cont-init
and loads that file over its process environment, so `plow-init` sets that one
name to the key it just published, `root:hermes 0640`. Every other name this
boot publishes — its endpoint, its home channel, its relay URL — is dropped
from the file rather than carried across, since a
persisted copy of any of them would be a stale shadow that wins the same
precedence fight: a reused fleet
home answering as the tenant before it. Two names this boot does *not* publish
go with them — `PLOW_CHAT_TOKEN` and `PLOW_CHAT_BASE_URL`, legacy spellings
of the credential and endpoint — because an old value left
readable there under a name nothing reads on purpose is how an agent
came to authenticate with a revoked token. The other `PLOW_CHAT_*` names are
chat-directory data with live consumers, and stay. Everything else in the file
— a bind-mounted fleet home's own configuration — is left exactly as it was
found. Both sources then agree on `API_SERVER_KEY`, and the value is still
regenerated on every boot.

```json
{
  "line": {"uid": "ln_…", "display_name": null, "provider_key": "+1…"},
  "chats": [
    {"uid": "cht_…", "status": "active", "participants": [
      {"type": "agent",  "relationship": "self", "line": {"uid": "ln_…"}},
      {"type": "member", "uid": "…", "role": "owner"}
    ]}
  ],
  "mcp_url": null
}
```

Every key is always present and a nullable one is null rather than omitted,
which is not how Plow's general chat and line endpoints serialize — so the
image requires all three and treats a body missing any of them as not an
identity. `line` is this agent's own line, and the home chat has to be on it:
a mailbox carrying the agent's persona is another line the credential opens,
and an owner alone with it looks like the home chat otherwise. `mcp_url` is
the relay endpoint, or null when the tenant has none.

The identity is re-asked on every boot,
so a home channel or a relay that moved moves with it — and a relay that went
away is switched off rather than left behind.

There is no fallback behind that fetch. Before identity is established, no
connection, a timeout, a 429 or a 5xx gets bounded retries, then parks. Once
waiting for first contact, those transient failures return to the poll loop
indefinitely: an onboarding wait can outlast a boot retry budget. An agent that cannot be told
who it is refuses to start rather than start as whoever it was last time: a
recorded identity belongs to the credential it was recorded under, and a home
volume outlives its tenant, so reusing one is how a new tenant lands in the
previous one's chat. Plow **answering** that this agent is gone — a 404 — or
answering with something that is not an identity, fails immediately without the
retries. A 401 or 403 is the same answer about the credential, but a rotated one
is refused for around a minute before it takes, and the restart after a rotation
is the boot that asks — so those are retried for two minutes, logging each wait,
and then park. Refused means refused for two minutes.

The same goes for the environment itself: no `PLOW_API_BASE` (and no
transition file, below), and nothing starts. `plow-init` is a oneshot every service depends on.

### How it refuses: the container parks

`plow-init` never exits. It writes the reason to stderr and — while it is still
root, which is every refusal that names a cause — to `/run/plow-init.parked`,
then blocks forever. The gateway declares this oneshot a dependency and s6-rc
starts it only when the oneshot *completes*, so a parked `plow-init` serves
nothing. Fail-closed never rested on the exit code.

Exiting would be worse than useless: this image's CMD *is* PID 1 on a microVM
host, where an exiting `/init` is `Attempted to kill init` — a panicked kernel
pinning a vCPU with no sshd. `S6_BEHAVIOUR_IF_STAGE2_FAILS=1` is set for the
same reason, and wrapping PID 1 to catch the exit is not open either:
s6-overlay's `/init` refuses to run unless it is pid 1.

That makes `plow-init` the boot's one gate. It verifies what the gateway needs
rather than trusting an earlier step — that the agent account exists and the
home is a directory — and parks with a precise reason otherwise. A
cont-init failure it does not depend on stays a warning; nothing that step
touched can serve anyone.

Plow's warm-pool VMs reach this by design: created with no environment, they
exist only to hold the image in the host's cache, and a parked container is
their healthy steady state.

## The two environment knobs

Everything about the tenant comes from `PLOW_API_BASE` and from Plow's answer.
`HERMES_PROVIDER` and `HERMES_MODEL` are the exception: they choose where inference
goes, which is an operator's decision about this container, not a fact about
the agent's identity. `plow-init` writes `model.provider` from the first.

`model.default` follows the provider. Switching **away** from Plow needs both
knobs — a model id belongs to the provider it was written for, so a name left
over from Plow means nothing to Anthropic. Coming back to Plow needs neither:
with `HERMES_PROVIDER=plow` and no `HERMES_MODEL`, the model is restored from
the image's own seed, along with the endpoint and key that describe Plow. That
is what keeps a switch back from being an edit — you do not have to remember
the model you were on before you left.

## Prompt caching

Plow's `/v1/chat/completions` is a LiteLLM proxy in front of Anthropic and
honours `cache_control`, but Hermes will not infer that: on the OpenAI wire it
grants caching only to a route whose provider id or hostname reads as LiteLLM,
and a config-defined provider is `custom` at runtime whatever this image's
config calls it — so neither signal can match, and every turn re-billed the
whole prefix at full price. The way in is the per-model declaration
(`providers.<provider>.models.<id>.prompt_caching: true`), which Hermes matches
on the endpoint and the model id rather than on a name — and `plow-init` writes
both halves of that match on every boot: the expanded `base_url`, and the flag
under whichever model is actually selected (`HERMES_MODEL` when it is set, the
seed's otherwise). The seed declares neither, and could not: the match is
against the URL the agent dials, and the seed's own `${PLOW_API_BASE}`
reference — credential-free by design — is never equal to it, so a declaration
written there is unreachable while looking perfectly set. Measured on a 15-turn
agent conversation: a repeat turn costs $0.0057 against $0.0430 uncached at the
same ~19k prompt.

## Identity

Hermes reads `$HERMES_HOME/SOUL.md` as the agent's identity. This image does
not ship that file; `plow-init` writes it on every boot from
`/opt/hermes/plow-seed/SOUL.md` — the base persona, `image/seed/SOUL.md` in
this repo — followed by `/opt/hermes/plow-seed/persona.md` when a variant ships
one. Root-owned 0644 in a sticky home, so a turn can neither rewrite nor unlink
it, and rewritten every boot so a populated volume never shadows a newer image.
**The base persona reaches every deployed agent on its next base-pin bump.** It
says only what is true of every Plow agent; a variant's `persona.md` says only
what is specific to that agent; what has to be said per turn about the chat
platform (Latch routing, group-room disclosure) belongs in `hermes-plugin-plow`,
not here. One owner per rule: before adding a sentence to the base persona, grep
the plugin's prompt constants for it.

The sticky home protects what root owns and nothing else:
`config.yaml` is handed to the agent on purpose — the chat plugin has to
rewrite it — so the agent can delete it or put something else in its place, and
`skills/` likewise. A **missing** `config.yaml` is seeded from the image's own
copy — cont-init writes one when the home has none, which is what stops the
runtime seeding a default with no chat platform in it. A **damaged** one is not
repaired: `plow-init` reads it only to re-assert what the image owns — Plow's
endpoint, model, provider entry and relay entry (the credential's variable name above all), the retry budget, the cron drift-guard switch and cron provider, the `tool_search` switch, the `terminal.cwd`, and every
seeded `display` value — on
every boot, and touches nothing else, so whatever else the agent leaves at that
path is its own to answer for. A deleted
skill is the same — the runtime records that deletion and honours it.

`plow-init` is a oneshot and every service depends on it, so anything it
refuses starts nothing — better a box that visibly never came up than one
answering with half its configuration.

## Latch's instructions

When the account has a Mac, `plow-init` asks the relay for its MCP `initialize`
result once per boot and writes its `instructions` whole to
`$HERMES_HOME/HERMES.md`, root-owned 0644 — a plow-init-managed file, the way
`SOUL.md` is written every boot, not the agent's to author. Hermes reads it from
`terminal.cwd` (the seed points it at the home) into the prompt's context tier,
above every plugin section — where Latch's own routing rule has to sit for a
fresh agent to read the owner's Mac instead of reporting its own empty stores
([#72](https://github.com/plow-pbc/plow-hermes-agent/issues/72)). No Mac, or a
fetch that fails, removes the file so no stale routing survives; the fetch never
stops the boot.

## Building a variant image

A variant is a persona plus skills — a separate repository whose Dockerfile
starts from this image and adds nothing else:

```dockerfile
FROM public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-<sha>

# Identity: only what is specific to this agent. plow-init writes the home's
# SOUL.md on every boot as the base persona followed by this file. Do not COPY
# anything to /var/lib/hermes/SOUL.md — it is overwritten at boot.
COPY --chown=0:0 persona.md /opt/hermes/plow-seed/persona.md
# The mode in its own step: `COPY --chmod=` is BuildKit-only, and a stock
# Docker still selects the legacy builder, where it fails the build outright.
RUN chmod 0644 /opt/hermes/plow-seed/persona.md

COPY --chown=10000:10000 skills/ /var/lib/hermes/skills/
# Both copies, as the base image does. The second is what a home that starts
# empty is seeded from, and what later image updates reach. It is a source,
# not a backup: a skill the agent deleted is recorded as deleted and is not
# re-added, and everything under /var/lib/hermes/skills is the agent's to
# change.
COPY --chown=10000:10000 skills/ /opt/hermes/skills/

```

The composed identity exists only after boot, so to inspect a published
variant read `docker run <tag> cat /opt/hermes/plow-seed/persona.md`, or boot it.

A variant that needs a background job adds its own s6 longrun under
`/etc/s6-overlay/s6-rc.d/`, with `plow-init` in its `dependencies.d/` and its
name in `user/contents.d/`.

A variant that needs environment of its own — a timezone, say, which is a
property of the tenant rather than of the credential — adds an s6 oneshot that
writes `/run/s6/container_environment/<NAME>`, the same way `plow-init`
publishes the values it owns.

Don't fight the init: nothing starts the gateway by hand — the dependency
already orders it after first boot — no credentials in `config.yaml`, no
inbound listener, and pin this image by digest or by an immutable `base-<sha>`
tag.

## The first USER.md

Hermes injects `$HERMES_HOME/memories/USER.md` into every prompt, and the model
reads it as what it knows about its owner. On a boot where the file does not
exist, `plow-init` stages a complete file and hard-links it into place -- so it
only ever appears whole, and once one exists the link fails rather than
overwrites: the owner's display name when Plow sends a real one (never the
phone or email handle Plow falls back to), that other Plow lines and earlier
agents left their work in Messages and mail on that Mac rather than in this
agent's sessions ([#73](https://github.com/plow-pbc/plow-hermes-agent/issues/73)). Where the
owner's world lives and how to reach it is Latch's own routing, carried once by
`HERMES.md` (#74) rather than restated here. General wording, no owner history.
An existing file is never touched, so a name change does not re-seed it.

## The plugin pin

The `plow_chat` plugin lives in
[plow-pbc/hermes-plugin-plow](https://github.com/plow-pbc/hermes-plugin-plow) and is
never vendored here. The Dockerfile pins one commit and fetches it at build
time; the same ARG feeds the `co.plow.plow-chat-plugin.revision` label, so the
plugin in the image and the plugin named by the image cannot drift apart.
Moving it is one line:

```dockerfile
ARG PLOW_CHAT_PLUGIN_SHA=<40-character commit sha>
```

## Publishing

Published by CI in `plow-pbc/plow` (`.github/workflows/build-agent-image.yml`),
triggered by a revision bump in `api/cloud-agents/agents.json`, or by a manual
run of that same workflow — never by a push from a developer's machine. The
tag is `public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-<full commit sha>`, one
immutable tag per commit.

One repository holds this image and every variant image built from it, so a tag
has to say which commit it came from: `base-` plus the **full 40-character SHA
of the commit in the repository that built it** — this one for the base image,
the variant's own for a variant. The tag does not name the variant, and there is
no `latest`.

Which SHA a given agent runs is not recorded here. Plow pins it per provider in
`api/cloud-agents/agents.json` in `plow-pbc/plow`, and composes the image
reference from it; publishing a tag makes it available, that file is what makes
it live.

The tags that exist are readable from the registry itself. On the web:
<https://gallery.ecr.aws/e1h7x4a2/plow-cloud-agents>. From a shell with no AWS
credential at all — the repository is public, so an anonymous pull token is
enough:

```sh
token=$(curl -fsSL \
  'https://public.ecr.aws/token/?service=public.ecr.aws&scope=repository:e1h7x4a2/plow-cloud-agents:pull' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -fsSL -H "Authorization: Bearer $token" \
  https://public.ecr.aws/v2/e1h7x4a2/plow-cloud-agents/tags/list
```

### Transition: the credential file

Until plow#2007 is live, a VM provisioned the old way gets no `PLOW_API_BASE`
in its environment. For that VM only — `PLOW_API_BASE` absent — `plow-init`
falls back to the old path: it runs `/exe.dev/setup` as root if it is
executable (and deletes it, so a reboot cannot replay it), then reads
`/var/lib/plow/credentials`, a `root:root` `0600` or `0400` file holding
`PLOW_API_BASE`, `PLOW_AGENT_TOKEN` and optional `AGENT_ID` and nothing else.
The file is its only source; the environment cannot outrank it. The fallback
is marked `TRANSITION` in `plow-init` and goes once plow#2007 is live and every
such VM has been re-provisioned.

## Try it

[`plow-agents`](https://github.com/plow-pbc/plow-agents) writes
`./plow-credentials`, a `KEY=VALUE` file with `PLOW_API_BASE` and
`PLOW_AGENT_TOKEN`, and `compose.yml` loads it as the container's environment
(`env_file`), pointed straight at Plow:

```sh
plow-agents login --new-line   # once per account
plow-agents mint <line-uid>    # writes ./plow-credentials
docker compose up --build -d
```

## Tests

`plow-init` decides where it reads Plow's endpoint from, what it does with each
answer from Plow, and what it writes into the agent's config. Those decisions
are checked without booting anything:

```sh
uv run --with pydantic --with pydantic-settings --with pyyaml --with pytest pytest
```

Run it from the repository root; there is no packaging to install. `plow-init`
is a hyphenated path rather than an importable module, so the test file loads
it by path with `importlib` and exercises the real functions.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 The Plow Collective, Inc.

"Plow" and the Plow logo are trademarks of The Plow Collective, Inc. The license grants no trademark rights.
