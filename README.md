# plow-hermes-agent

The base OCI image for a hosted Plow agent.

A Plow agent is a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
supervised by [s6-overlay](https://github.com/just-containers/s6-overlay),
reachable only through Plow Chat. One image serves two paths: exe.dev unpacks it
into a VM rootfs and boots its `Cmd`, and `docker run` boots the same `Cmd` in a
container — `/init` either way, so what the developer runs is what the tenant
gets. The image is credential-free and tenant-free — provisioning writes
`/var/lib/hermes/.env` and flips one YAML boolean; it installs no code.

Built for `linux/amd64` and `linux/arm64`, so an Apple Silicon Mac runs it
natively rather than under emulation.

## What is in the image

| path | what it is |
|---|---|
| `/var/lib/hermes/` | the agent's home (`HERMES_HOME` and `HERMES_WRITE_SAFE_ROOT`, set as image ENV so everything in the image agrees on it), `3770 root:hermes` — `config.yaml` (overrides only, every tenant value a `${...}` reference), `SOUL.md` (the identity, root-owned), `skills/`, `plugins/plow_chat/` |
| `/usr/local/lib/plow/first-boot.sh` | the ownership and mode work, then the `first-boot.d/*.sh` drop-ins |
| `/etc/s6-overlay/s6-rc.d/plow-init/` | oneshot: runs the host's `/exe.dev/setup` once if it is there, then `first-boot.sh` |
| `/etc/s6-overlay/s6-rc.d/hermes-gateway/` | longrun: the gateway as uid 10000, depending on `plow-init` |
| `/usr/local/bin/plow-restart-gateway` | restarts the gateway through the supervisor and waits for the listener |

## Identity

Hermes reads `$HERMES_HOME/SOUL.md` as the agent's identity. This image ships
one, at `/var/lib/hermes/SOUL.md`, root-owned in a sticky home so a turn can
neither rewrite nor unlink it. A variant replaces or extends it in its own
layer — see below; first boot re-asserts root ownership either way.

A `first-boot.d` hook that fails fails first boot. `plow-init` is a oneshot and
every service depends on it, so nothing starts — better a box that visibly never
came up than one answering with half its configuration.

## Building a variant image

A variant is a persona plus skills — a separate repository whose Dockerfile
starts from this image and adds nothing else:

```dockerfile
FROM public.ecr.aws/e1h7x4a2/plow-hermes-agent:base-<sha>

# Identity — replace it outright:
COPY --chown=10000:10000 --chmod=0600 SOUL.md /var/lib/hermes/SOUL.md

# ...or extend the base one instead:
#   COPY --chown=10000:10000 persona.md /tmp/persona.md
#   RUN printf '\n' >> /var/lib/hermes/SOUL.md \
#    && cat /tmp/persona.md >> /var/lib/hermes/SOUL.md \
#    && rm /tmp/persona.md

COPY --chown=10000:10000 skills/ /var/lib/hermes/skills/

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
`public.ecr.aws/e1h7x4a2/plow-hermes-agent:base-<full commit sha>` — one
immutable tag per commit. Plow's own deploy tooling moves the blessed
`hermes-prod` tag; publishing a `base-<sha>` tag blesses nothing.
