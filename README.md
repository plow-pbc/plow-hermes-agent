# plow-hermes-agent

The base OCI image for a hosted Plow agent.

A Plow agent is a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
running natively under systemd on its own small VM, reachable only through Plow
Chat. **There is no container on the agent's VM**: the host unpacks this image
straight into the VM rootfs and boots `/sbin/init`. The image is credential-free
and tenant-free — provisioning writes `/var/lib/hermes/.env` and flips one YAML
boolean; it installs no code.

## What is in the image

| path | what it is |
|---|---|
| `/var/lib/hermes/` | the agent's home (`HERMES_HOME`), uid 10000, mode 0700 — `config.yaml` (overrides only, every tenant value a `${...}` reference), `SOUL.md` (the identity, mode 0600), `skills/`, `plugins/plow_chat/` |
| `/usr/local/lib/plow/first-boot.sh` | runs the `first-boot.d/*.sh` drop-ins |
| `/etc/systemd/system/` | `agent-setup.service` (runs the host's `/exe.dev/setup` once) and `hermes-gateway.service`, which `Requires=` it |

## Identity

Hermes reads `$HERMES_HOME/SOUL.md` as the agent's identity. This image ships
one, at `/var/lib/hermes/SOUL.md`, uid 10000 mode 0600. A variant replaces or
extends it in its own layer — see below.

`first-boot.sh` does not touch identity; it only runs the `first-boot.d`
drop-ins, and a hook that fails fails first boot, which keeps the gateway from
ever starting — better a VM that visibly never came up than one answering with
half its configuration.

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

Don't fight the init: no `systemctl start hermes-gateway` from the setup script
(it deadlocks against `Requires=agent-setup.service`; the ordering already covers it), no credentials in
`config.yaml`, no inbound listener, and pin this image by digest or by an
immutable `base-<sha>` tag.

## Build and check

Requires Docker with buildx. No credential, no pre-steps.

```sh
scripts/check-image.sh          # build, then import the plugin inside the image
docker build .                  # build only
```

`check-image.sh` imports `plow_chat` inside the built image — same file,
interpreter and uid the gateway uses. Nothing else couples the plugin to the
runtime: the runtime is pinned by digest, the plugin moves in its own
repository, and a mismatch builds clean and boots deaf.

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
