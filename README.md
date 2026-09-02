# plow-hermes-agent

The base OCI image for a hosted Plow agent.

A Plow agent is a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
supervised by [s6-overlay](https://github.com/just-containers/s6-overlay),
reachable only through Plow Chat. One image serves two paths: exe.dev unpacks it
into a VM rootfs and boots its `Cmd`, and `docker run` boots the same `Cmd` in a
container — `/init` either way, so what the developer runs is what the tenant
gets. The image is credential-free and tenant-free — two lines land at
`/var/lib/plow/credentials` and the image does the rest; it installs no code.
There is no local mode: a developer's machine writes that same file and gets
the same boot, which is what makes the one path worth checking.

Running one is [`plow-pbc/plow-agents`](https://github.com/plow-pbc/plow-agents)
— a compose file, a credential, and any image that meets the contract below.
Nothing in this repository is about running it.

The bake builds `linux/amd64` and `linux/arm64`, so an Apple Silicon Mac can
run it natively; the tags published so far carry `amd64` alone.

## What is in the image

| path | what it is |
|---|---|
| `/var/lib/hermes/` | the agent's home (`HERMES_HOME` and `HERMES_WRITE_SAFE_ROOT`, set as image ENV so everything in the image agrees on it), `3770 root:hermes` — `config.yaml` (overrides only, every tenant value a `${...}` reference), `SOUL.md` (the identity, root-owned), `skills/` |
| `/opt/hermes/plugins/plow_chat/` | the chat plugin, bundled rather than seeded into the home, so the agent's phone line does not live in a directory the agent can write |
| `/opt/hermes/skills/` | the same seed skills again, out of the agent's reach; the gateway seeds them into a home that lacks them and updates the ones the agent has not customised. A skill the agent deleted stays deleted — the runtime records that and honours it |
| `/usr/local/lib/plow/first-boot.sh` | the ownership and mode work, then the `first-boot.d/*.sh` drop-ins |
| `/var/lib/plow/credentials` | not shipped — the host's drop-in, if there is one; see below |
| `/var/lib/plow/credentials.host` | not shipped either — the same file bind-mounted from a developer's machine, promoted into the one above at cont-init |
| `/etc/s6-overlay/s6-rc.d/plow-init/` | oneshot: runs the host's `/exe.dev/setup` once if it is there, then `first-boot.sh`, then reads the credential drop-in and renders the dotenv |
| `/etc/cont-init.d/00-plow-sanitize` | takes away anything wearing `config.yaml`'s name, and promotes a bind-mounted credential into the drop-in path |
| `/etc/s6-overlay/s6-rc.d/hermes-gateway/` | longrun: the gateway as uid 10000, depending on `plow-init` |
| `/usr/local/bin/plow-restart-gateway` | restarts the gateway through the supervisor and waits for the listener |

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
`GET $PLOW_API_BASE/v1/agents/cloud/me` answers with the home channel and the
relay endpoint. From those, the image renders `/var/lib/hermes/.env` itself,
deriving the inference key alias, generating an `API_SERVER_KEY` on first boot
and defaulting `TZ` to UTC.

The drop-in wins over the persisted dotenv, and the file is left in place: a
rotation is a rewrite of those two lines and a restart, with no shell into the
agent. The identity is re-asked on every boot, so a home channel or a relay
that moved moves with it — and a relay that went away is switched off rather
than reinstated from the copy the home kept.

Plow being unreachable is not an outage the agent has to share. An agent that
already ran comes up on the identity its home recorded, provided that identity
was recorded against the very token the drop-in is holding; it says so in the
log. A first boot, or a token rotated while Plow was down, has no such record
and refuses to start rather than adopt one that belongs to somebody else —
`plow-init` is a oneshot every service depends on, so nothing starts.

That fallback is for *silence* only: no connection, no answer in time, a 429 or
a 5xx. If Plow **answers** that the credential is not this agent's — a 401, 403
or 404 — or answers with something that is not an identity, the recorded one is
precisely what must not be reused, and the boot fails closed however well the
token matches.

### From a developer's machine

The same file, and the same rules — but a bind mount carries its host's
ownership and mode into the container, which on a Linux host is never
`root:root 0600`. Relaxing the gate for that would be relaxing it for the VM
too, so the mount lands beside the drop-in instead, at
`/var/lib/plow/credentials.host`, and `00-plow-sanitize` copies it as root into
`/var/lib/plow/credentials` at `0600` before anything reads either. The mount
itself is never written; a rotation is a rewrite of it and a restart, and only
a mount newer than the copy is promoted again.

`plow-pbc/plow-agents` is the compose file that does this, and the tool that
writes the two lines.

## The two environment knobs

Everything about the tenant comes from the drop-in and from Plow's answer.
`HERMES_PROVIDER` and `HERMES_MODEL` are the exception, and are read from the
container environment rather than from that file: they choose where inference
goes, which is an operator's decision about this container, not a fact about
the agent's identity. `plow-config` writes `model.provider` from the first, and
`model.default` only when the second is set — so a provider switch needs both,
because a model id belongs to the provider it was written for.

A credential file naming either is refused: the allowlist for a drop-in is
`PLOW_API_BASE` and `PLOW_AGENT_TOKEN`, and nothing else.

## Identity

Hermes reads `$HERMES_HOME/SOUL.md` as the agent's identity. This image ships
one, at `/var/lib/hermes/SOUL.md`, root-owned in a sticky home so a turn can
neither rewrite nor unlink it. That protects what root owns and nothing else:
`config.yaml` is handed to the agent on purpose — the chat plugin has to
rewrite it — so the agent can delete it or put something else in its place, and
`skills/` likewise. `config.yaml` is covered by restoration rather than
permission: first boot puts back one that is missing, not a regular file, or
not this image's config, from a copy outside every home. A deleted skill is not
covered at all — the runtime records that deletion and honours it. A variant replaces or extends `SOUL.md` in its own
layer — see below; first boot re-asserts root ownership either way.

A `first-boot.d` hook that fails fails first boot. `plow-init` is a oneshot and
every service depends on it, so nothing starts — better a box that visibly never
came up than one answering with half its configuration.

## Building a variant image

A variant is a persona plus skills — a separate repository whose Dockerfile
starts from this image and adds nothing else:

```dockerfile
FROM public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-<sha>

# Identity — replace it outright:
COPY --chown=10000:10000 --chmod=0600 SOUL.md /var/lib/hermes/SOUL.md

# ...or extend the base one instead:
#   COPY --chown=10000:10000 persona.md /tmp/persona.md
#   RUN printf '\n' >> /var/lib/hermes/SOUL.md \
#    && cat /tmp/persona.md >> /var/lib/hermes/SOUL.md \
#    && rm /tmp/persona.md

COPY --chown=10000:10000 skills/ /var/lib/hermes/skills/
# Copy them to /opt/hermes/skills/ as well, so a home that starts empty gets
# them and later image updates reach them. It is a source, not a backup: a
# skill the agent deleted is recorded as deleted and is not re-added, and
# everything under /var/lib/hermes/skills is the agent's to change.

# First-boot work, if any. A drop-in, NOT a replacement for first-boot.sh.
COPY --chmod=0755 first-boot.d/50-variant.sh /usr/local/lib/plow/first-boot.d/50-variant.sh
```

A variant that needs a background job adds its own s6 longrun under
`/etc/s6-overlay/s6-rc.d/`, with `plow-init` in its `dependencies.d/` and its
name in `user/contents.d/`.

Don't fight the init: nothing starts the gateway by hand — the dependency
already orders it after first boot — no credentials in `config.yaml`, no
inbound listener, and pin this image by digest or by an immutable `base-<sha>`
tag.

## Restarting the gateway

`plow-restart-gateway` — it signals the supervisor and waits until a new
process is answering on `127.0.0.1:8642`. `systemctl restart hermes-gateway`
is bridged to it for callers written against the systemd image, and refuses
every other command rather than returning 0 for work it did not do.

## Build and check

Requires Docker with buildx. No credential, no pre-steps.

```sh
scripts/check-image.sh                        # linux/amd64, what the VM host unpacks
PLATFORM=linux/arm64 scripts/check-image.sh   # the architecture a Mac runs natively
PLOW_REVISION=$(git rev-parse HEAD) docker buildx bake base   # both, one index
```

`check-image.sh` builds and then boots the image, and every check is for a
failure that builds clean and boots looking healthy:

- `plow_chat` imports — same file, interpreter and uid the gateway uses. The
  runtime is pinned by digest and the plugin moves in its own repository, so a
  mismatch builds clean and boots deaf.
- the write guard follows the home — pointed elsewhere, every write the agent
  makes into its own directory is denied and nothing says so at boot.
- s6 is PID 1 and the gateway is listening on `127.0.0.1:8642` — the same
  loopback listener Plow's provisioning waits for.
- the home, `skills/` and `SOUL.md` still have the ownership the hardening
  depends on after first boot, and a second boot changes nothing. Both matter:
  the runtime bootstraps whatever home it is handed, and on the cloud path
  first boot runs twice.
- the restart Plow's credential update performs hands back a NEW process that
  is listening — otherwise an update verifies the credential the old process is
  still holding.
- the whole shape end to end, against a stubbed identity endpoint — and it is
  the shape every other check above runs in, because there is only one: a boot
  with nothing but the credential drop-in renders the whole dotenv and comes up
  with the relay on, a rewrite of that file plus a restart puts the new token in
  the gateway's own environment, and a withdrawn relay switches off rather than
  surviving in the home.
- a drop-in that arrived as a bind mount — uid 10000, mode 0644, what a Linux
  host's own file looks like from in here — is promoted to a root:root 0600
  copy and boots, the mount itself unchanged, and a rewrite of it rotates the
  credential.
- an unreachable Plow does not take a provisioned agent down: it boots on the
  identity its home recorded for that same credential — and refuses when the
  credential has been rotated since, rather than adopting an identity that was
  recorded for a different one.
- silence and refusal are told apart: a 503 or a dead socket falls back, while a
  404 for the same unchanged token, or an answer carrying no `chat_uid`, starts
  nothing.
- a drop-in that cannot be used starts nothing: Plow unreachable on a first
  boot, a name the contract does not allow, or a file that is not root:root at
  0600 (or 0400) — 0644, 0620 and 0602 are each refused by name.
- a `first-boot.d` hook that exits non-zero, and a boot with no
  `PLOW_AGENT_TOKEN`, both leave the gateway unstarted and `/init` exiting
  non-zero. The second matters because the gateway comes up perfectly well
  without a credential: it serves its API and runs cron with no adapter
  attached, and no owner can reach it.

Multi-platform bake needs a `docker-container` builder
(`docker buildx create --driver docker-container`); the default driver builds
one architecture at a time.

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
