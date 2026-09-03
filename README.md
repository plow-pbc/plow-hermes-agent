# plow-hermes-agent

The base OCI image for a hosted Plow agent.

A Plow agent is a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
supervised by [s6-overlay](https://github.com/just-containers/s6-overlay),
reachable only through Plow Chat. One image serves two paths: exe.dev unpacks it
into a VM rootfs and boots its `Cmd`, and `docker run` boots the same `Cmd` in a
container — `/init` either way, so what the developer runs is what the tenant
gets. The image is credential-free and tenant-free — two lines land at
`/var/lib/plow/credentials` and the image does the rest. It adds one package
to the runtime's environment (`pydantic-settings`, pinned, `--no-deps`) and no
code of its own beyond the init below.
There is no local mode: a developer's machine writes that same file and gets
the same boot, which is what makes the one path worth checking.

Running one is [`plow-pbc/plow-agents`](https://github.com/plow-pbc/plow-agents)
— a compose file, a credential, and any image that meets the contract below.
Nothing in this repository is about running it.

`docker build` produces the architecture you are on; the tags published so far
carry `amd64` alone.

## What is in the image

| path | what it is |
|---|---|
| `/var/lib/hermes/` | the agent's home (`HERMES_HOME` and `HERMES_WRITE_SAFE_ROOT`, set as image ENV so everything in the image agrees on it), `3770 root:hermes` — `config.yaml` (overrides only, every tenant value a `${...}` reference), `SOUL.md` (the identity, root-owned), `skills/` |
| `/opt/hermes/plugins/plow_chat/` | the chat plugin, bundled rather than seeded into the home, so the agent's phone line does not live in a directory the agent can write |
| `/opt/hermes/skills/` | the same seed skills again, out of the agent's reach; the gateway seeds them into a home that lacks them and updates the ones the agent has not customised. A skill the agent deleted stays deleted — the runtime records that and honours it |
| `/var/lib/plow/credentials` | not shipped — the host's drop-in, if there is one; see below |
| `/var/lib/plow/credentials.host` | not shipped either — the same file bind-mounted from a developer's machine, promoted into the one above at cont-init |
| `/etc/s6-overlay/scripts/plow-init.py` | the oneshot: repairs the home's ownership, reads the credential, asks Plow who this agent is, publishes the answer, and edits the config as the agent |
| `/etc/cont-init.d/00-plow-sanitize` | seeds `config.yaml` if the home has none, and promotes a bind-mounted credential into the path below |
| `/etc/s6-overlay/s6-rc.d/hermes-gateway/` | longrun: the gateway as uid 10000, depending on `plow-init` |

## The credential drop-in

Provisioning's whole involvement with a tenant's VM is one file, `root:root`
`0600` (`0400` is accepted too; nothing looser is):

```
PLOW_API_BASE=https://api.plow.dev
PLOW_AGENT_TOKEN=<the agent's own credential>
```

`plow-init` reads it as data — never sourced, and only those two names, so a
provisioner that has drifted ahead of the image is refused rather than
half-obeyed — and then asks Plow the rest with that credential:
`GET $PLOW_API_BASE/v1/agents/cloud/me` answers with this agent's line, the
chats it is in, and a relay endpoint. Plow does not name a home channel, so the
image picks one: the active chat holding exactly this agent and exactly one
member, who is the owner. Zero matches or several stops the boot, printing the
roster it saw — the home channel is where the agent answers, and the wrong one
is an agent talking to the wrong people. From that, the image publishes the
tenant's environment
itself — one file per name under `/run/s6/container_environment`, which every
service inherits — deriving the inference key alias from the credential and
generating a fresh `API_SERVER_KEY` on every boot.

The tenant's credential is never written to disk inside the container. The
loopback `API_SERVER_KEY` is the one exception, and only because it has to be:
the runtime writes a key of its own into `$HERMES_HOME/.env` during cont-init
and loads that file over its process environment, so `plow-init` overwrites
that file with the key it just published — one name, `root:hermes 0640`. Both
sources then agree, and the value is still regenerated on every boot.

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
identity. `line` is this agent's own line; the image does not read it, and
carries it only because it is part of that answer. `mcp_url` is the relay
endpoint, or null when the tenant has none.

Getting a token in the first place is `plow-agents`. First time on an account:

```sh
plow-agents login --new-line   # provisions the line and stores the account token
plow-agents mint <line-uid>    # the agent's own credential, scoped to that line
```

`login` is once per account; `mint` once per agent.

`plow-init` waits up to 60 seconds for that file to appear before giving up,
so a host may write it into a container that is already running; a file present
at boot costs nothing, because the first look finds it.

The file is the only source: a settings model would otherwise read the process
environment first, letting `docker run -e PLOW_AGENT_TOKEN=…` outrank the
credential the image was given, so every source but that file is switched off.
It is left in place, and a rotation is a rewrite of those two lines and a
restart, with no shell into the agent. The identity is re-asked on every boot,
so a home channel or a relay that moved moves with it — and a relay that went
away is switched off rather than left behind.

There is no fallback behind that fetch. Silence — no connection, no answer in
time, a 429 or a 5xx — is retried briefly, because a VM's network is not always
up when its first service is; it is not survived. An agent that cannot be told
who it is refuses to start rather than start as whoever it was last time: a
recorded identity belongs to the credential it was recorded under, and a home
volume outlives its tenant, so reusing one is how a new tenant lands in the
previous one's chat. Plow **answering** that the credential is not this
agent's — a 401, 403 or 404 — or answering with something that is not an
identity, fails immediately without the retries.

The same goes for the credential itself: no file, a file that is not a
root-owned regular file at 0600 or 0400, or one naming anything but those two
keys, and nothing starts. `plow-init` is a oneshot every service depends on.

### From a developer's machine

The same file, and the same rules — but a bind mount carries its host's
ownership and mode into the container, which on a Linux host is never
`root:root 0600`. Relaxing the gate for that would be relaxing it for the VM
too, so the mount lands beside the drop-in instead, at
`/var/lib/plow/credentials.host`, and `00-plow-sanitize` copies it as root into
`/var/lib/plow/credentials` at `0600` before anything reads either. The mount
itself is never written; a rotation is a rewrite of it and a restart, which
the next boot copies into place.

`plow-pbc/plow-agents` is the compose file that does this, and the tool that
writes the two lines.

### The host's own hook

If `/exe.dev/setup` exists and is executable, `plow-init` runs it as root
before it reads the credential, then deletes it so a reboot cannot replay it.
It is how a VM host does whatever it must do to a fresh machine — it is placed
by whoever built that machine, not by this image, and nothing puts one there on
a developer's own. An image that finds one runs it, so a host that can write
that path already owns the container.

## The two environment knobs

Everything about the tenant comes from the drop-in and from Plow's answer.
`HERMES_PROVIDER` and `HERMES_MODEL` are the exception, and are read from the
container environment rather than from that file: they choose where inference
goes, which is an operator's decision about this container, not a fact about
the agent's identity. `plow-init` writes `model.provider` from the first.

`model.default` follows the provider. Switching **away** from Plow needs both
knobs — a model id belongs to the provider it was written for, so a name left
over from Plow means nothing to Anthropic. Coming back to Plow needs neither:
with `HERMES_PROVIDER=plow` and no `HERMES_MODEL`, the model is restored from
the image's own seed, along with the endpoint and key that describe Plow. That
is what keeps a switch back from being an edit — you do not have to remember
the model you were on before you left.

A credential file naming either is refused: the allowlist for a drop-in is
`PLOW_API_BASE` and `PLOW_AGENT_TOKEN`, and nothing else.

## Identity

Hermes reads `$HERMES_HOME/SOUL.md` as the agent's identity. This image ships
one, at `/var/lib/hermes/SOUL.md`, root-owned in a sticky home so a turn can
neither rewrite nor unlink it. That protects what root owns and nothing else:
`config.yaml` is handed to the agent on purpose — the chat plugin has to
rewrite it — so the agent can delete it or put something else in its place, and
`skills/` likewise. A **missing** `config.yaml` is seeded from the image's own
copy — cont-init writes one when the home has none, which is what stops the
runtime seeding a default with no chat platform in it. A **damaged** one is not
repaired: nothing inspects the file's contents, and the edit that follows runs
as the agent's own user, so whatever the agent leaves at that path is its own
to answer for. A deleted
skill is the same — the runtime records that deletion and honours it. A variant replaces or extends `SOUL.md` in its own
layer — see below; first boot re-asserts root ownership either way.

`plow-init` is a oneshot and every service depends on it, so anything it
refuses starts nothing — better a box that visibly never came up than one
answering with half its configuration.

## Building a variant image

A variant is a persona plus skills — a separate repository whose Dockerfile
starts from this image and adds nothing else:

```dockerfile
FROM public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-<sha>

# Identity — replace it outright. Root-owned and readable: first boot
# re-asserts root ownership on this file and does not touch its mode, so a
# variant that ships it 0600 to uid 10000 ends up with an identity the agent
# cannot read.
COPY --chown=0:0 --chmod=0644 SOUL.md /var/lib/hermes/SOUL.md

# ...or extend the base one instead:
#   COPY --chown=10000:10000 persona.md /tmp/persona.md
#   RUN printf '\n' >> /var/lib/hermes/SOUL.md \
#    && cat /tmp/persona.md >> /var/lib/hermes/SOUL.md \
#    && rm /tmp/persona.md

COPY --chown=10000:10000 skills/ /var/lib/hermes/skills/
# Both copies, as the base image does. The second is what a home that starts
# empty is seeded from, and what later image updates reach. It is a source,
# not a backup: a skill the agent deleted is recorded as deleted and is not
# re-added, and everything under /var/lib/hermes/skills is the agent's to
# change.
COPY --chown=10000:10000 skills/ /opt/hermes/skills/

```

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

## The plugin pin

The `plow_chat` plugin lives in
[plow-pbc/hermes-plow-chat](https://github.com/plow-pbc/hermes-plow-chat) and is
never vendored here. The Dockerfile pins one commit and fetches it at build
time; the same ARG feeds the `co.plow.plow-chat-plugin.revision` label, so the
plugin in the image and the plugin named by the image cannot drift apart.
Moving it is one line:

```dockerfile
ARG PLOW_CHAT_PLUGIN_SHA=<40-character commit sha>
```

## Publishing

Not automated yet. Built and gated locally, pushed by hand to
`public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-<full commit sha>` — one
immutable tag per commit. Plow's own deploy tooling moves the blessed
`hermes-prod` tag; publishing a `base-<sha>` tag blesses nothing.

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

## Try it

Any credential works: the image asks Plow who holds it, so point it at a Plow
that will answer.

```sh
docker build -t plow-agent .
printf 'PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=<token>\n' > plow-credentials
chmod 600 plow-credentials
docker run --rm -v "$PWD/plow-credentials:/var/lib/plow/credentials.host:ro" plow-agent
```

`plow-agents` is the compose file and the tool that mints that credential.

## Tests

`plow-init` decides which credentials it will read, what it does with each
answer from Plow, and what it writes into the agent's config. Those decisions
are checked without booting anything:

```sh
uv run --with pydantic --with pydantic-settings --with pyyaml --with pytest pytest
```

Run it from the repository root; there is no packaging to install. `plow-init`
is a hyphenated path rather than an importable module, so the test file loads
it by path with `importlib` and exercises the real functions.
