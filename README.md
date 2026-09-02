# plow-hermes-agent

The base OCI image for a hosted Plow agent.

A Plow agent is a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
supervised by [s6-overlay](https://github.com/just-containers/s6-overlay),
reachable only through Plow Chat. One image serves two paths: exe.dev unpacks it
into a VM rootfs and boots its `Cmd`, and `docker run` boots the same `Cmd` in a
container — `/init` either way, so what the developer runs is what the tenant
gets. The image is credential-free and tenant-free — provisioning writes
`/var/lib/hermes/.env` and flips one YAML boolean; it installs no code.

The bake builds `linux/amd64` and `linux/arm64`, so an Apple Silicon Mac can
run it natively; the tags published so far carry `amd64` alone.

## Run locally

Docker, and a phone that can text. The agent runs against production Plow.

```sh
export PLOW_AGENT_IMAGE=public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-<sha>
docker compose run --rm agent plow-activate > .env   # prints a code; text it
docker compose up -d                                 # boots the agent
```

The compose default names a tag that does not exist, so an unset variable fails
on the pull rather than booting some other commit's image. List the real ones —
no AWS credential needed, the repository is public:

```sh
token=$(curl -fsSL \
  'https://public.ecr.aws/token/?service=public.ecr.aws&scope=repository:e1h7x4a2/plow-cloud-agents:pull' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -fsSL -H "Authorization: Bearer $token" \
  https://public.ecr.aws/v2/e1h7x4a2/plow-cloud-agents/tags/list
```

for example `base-9636490e7838eab89f62d10477febd07a4743907`. Each names the
commit it was built from; **Publishing** below says which repository that is.

`plow-activate` starts a Plow activation, prints the code and the number to
text it to, waits for that text, and writes the five variables the agent needs
to stdout -- which is why the redirect is the whole of "configure it". It then
narrows its own credential to that agent's line before printing anything, so
what lands in `.env` reaches this agent's chats and Plow's inference and
nothing else on the account. Prompts and progress go to stderr, so they stay
out of the file.

Then text the number the agent answers on, and it replies. `docker compose
logs -f agent` is what it is doing.

Every published tag is `amd64` only today, so a Mac runs one under emulation;
the local build below is native `arm64` there.

`.env.example` documents the five variables. `.env` holds live credentials and
is gitignored. `docker compose down -v` deletes the agent's home volume — its
sessions, memories and provider logins, with no second copy — but **not** the
`.env` beside this file: delete that yourself when you are done with the agent,
or the next `up` brings the same one back.

### Against a Plow stack on this machine

`compose.e2e.yml` builds this checkout instead of pulling, and joins the dev
stack's own network so the API is reached by container name — no host port to
guess, and no `PLOW_AGENT_IMAGE` to set:

```sh
docker compose -f compose.yml -f compose.e2e.yml run --rm agent \
  plow-activate --api-base http://api:8000 > .env
docker compose -f compose.yml -f compose.e2e.yml up -d
```

The activation code arrives at the local stack's iMessage twin instead of a
phone; deliver it there the way that stack documents.
`scripts/check-activate.sh` does the whole handshake and checks the credential
it produced.

## Change inference provider

Inference goes through Plow by default, on the same credential as chat. Any
provider Hermes supports works instead. Two lines and a login:

```sh
printf 'HERMES_PROVIDER=openai-codex\nHERMES_MODEL=gpt-5.5\n' >> .env
docker compose run --rm agent hermes auth add openai-codex   # device-code login
docker compose up -d
```

`HERMES_MODEL` is not optional when switching: the seeded default model id
belongs to the Plow provider and means nothing to another one.

A provider that takes an API key needs no login, just the variable it reads:

```sh
printf 'HERMES_PROVIDER=anthropic\nHERMES_MODEL=claude-sonnet-4-5\nANTHROPIC_API_KEY=sk-ant-...\n' >> .env
docker compose up -d
```

Switching back needs both lines, because a model id belongs to the provider it
was written for:

```sh
printf 'HERMES_PROVIDER=plow\nHERMES_MODEL=anthropic/claude-sonnet-5\n' >> .env
docker compose up -d
```

Boot writes `model.default` only when `HERMES_MODEL` is set, so dropping the
line leaves the previous provider's model id behind rather than restoring the
one the image shipped. Nothing else has to be restored: the Plow provider's own
block stays in `config.yaml` untouched the whole time, and the login
`hermes auth add` writes lives in the home volume and survives a switch away
and back.

## What is in the image

| path | what it is |
|---|---|
| `/var/lib/hermes/` | the agent's home (`HERMES_HOME` and `HERMES_WRITE_SAFE_ROOT`, set as image ENV so everything in the image agrees on it), `3770 root:hermes` — `config.yaml` (overrides only, every tenant value a `${...}` reference), `SOUL.md` (the identity, root-owned), `skills/` |
| `/opt/hermes/plugins/plow_chat/` | the chat plugin, bundled rather than seeded into a home, so it survives a volume or bind mount over `HERMES_HOME` |
| `/opt/hermes/skills/` | the same seed skills again, as bundled skills, which the gateway reconciles into `$HERMES_HOME/skills` for a runtime whose home is not the baked one |
| `/usr/local/lib/plow/first-boot.sh` | the ownership and mode work, then the `first-boot.d/*.sh` drop-ins |
| `/etc/s6-overlay/s6-rc.d/plow-init/` | oneshot: runs the host's `/exe.dev/setup` once if it is there, then `first-boot.sh` |
| `/etc/s6-overlay/s6-rc.d/hermes-gateway/` | longrun: the gateway as uid 10000, depending on `plow-init` |
| `/usr/local/bin/plow-restart-gateway` | restarts the gateway through the supervisor and waits for the listener |
| `/usr/local/bin/plow-activate` | mints this agent's Plow credential and prints its dotenv |

## Identity

Hermes reads `$HERMES_HOME/SOUL.md` as the agent's identity. This image ships
one, at `/var/lib/hermes/SOUL.md`, root-owned in a sticky home so a turn can
neither rewrite nor unlink it — the same pair protects `config.yaml`'s
ownership, and nothing deeper: entries the agent owns, `skills/` included, it
can still remove. A variant replaces or extends it in its own
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
# A variant whose agents run on a mounted home needs the bundled placement too
# -- `COPY skills/ /opt/hermes/skills/` -- because the mount hides this layer.
# That copy is also the only one a turn cannot touch: everything under
# /var/lib/hermes/skills is owned by the agent, so the sticky home does not
# stop it renaming a skill out of the scan path. The bundled tree is what puts
# it back on the next boot.

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
