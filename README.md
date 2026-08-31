# plow-hermes-agent

The base OCI image for a hosted Plow agent.

A Plow agent is a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
running natively under systemd on its own small VM, reachable only through Plow
Chat. This repository builds the one image that every such agent boots from: the
pinned Hermes runtime, the `plow_chat` platform plugin, a credential-free
`config.yaml`, the systemd units, the seed skills, and a first-boot hook.

**There is no container on the agent's VM.** The host unpacks this image
straight into the VM rootfs and boots `/sbin/init`; `hermes-gateway.service` runs
the gateway as uid 10000 out of `/opt/hermes`. Everything below follows from
that.

## What is in the image

| path | what it is |
|---|---|
| `/var/lib/hermes/` | the agent's home (`HERMES_HOME`), uid 10000, mode 0700 |
| `/var/lib/hermes/config.yaml` | overrides only; every tenant value is a `${...}` reference |
| `/var/lib/hermes/soul.d/00-base.md` | base identity — see [Soul composition](#soul-composition) |
| `/var/lib/hermes/skills/` | seed skills available to every agent |
| `/var/lib/hermes/plugins/plow_chat/` | the Plow Chat platform adapter |
| `/var/lib/hermes/.env` | **not in the image** — written on first boot |
| `/usr/local/lib/plow/first-boot.sh` | composes the soul, then runs `first-boot.d/*.sh` |
| `/usr/local/lib/plow/first-boot.d/` | empty; where a downstream image adds first-boot work |
| `/etc/systemd/system/exe-setup.service` | runs the host-supplied `/exe.dev/setup` once, before the gateway |
| `/etc/systemd/system/hermes-gateway.service` | `hermes gateway run`, `Requires=exe-setup.service` |

The image is credential-free and tenant-free by construction: one public image
serves every tenant. Provisioning writes `/var/lib/hermes/.env` and flips one
YAML boolean; it installs no code.

Two labels record provenance on every build:

- `org.opencontainers.image.revision` — the commit of this repository
- `co.plow.plow-chat-plugin.revision` — the commit of the plugin inside it

## The plugin pin

The `plow_chat` plugin's canonical home is
[plow-pbc/hermes-plow-chat](https://github.com/plow-pbc/hermes-plow-chat), and
it is the only copy. This repository does not vendor it. It pins one commit:

```
plow-chat-plugin.ref     a full 40-character commit SHA, nothing else
```

`scripts/plugin-pin.sh` is the only reader of that file, and both the staging
script and the image label call it — so the plugin *in* the image and the plugin
*named by* the image cannot drift apart. It is strict: the whole file must be 41 bytes
— 40 lowercase hex characters and one trailing newline — and it is compared
against a reconstruction of the SHA it parsed, so a trailing byte cannot survive
a check that read only the first line. **There is no override.** Moving
the plugin is a one-line pull request against `plow-chat-plugin.ref`.

Staging needs no credential — hermes-plow-chat is public — so anyone can build
and gate this image without access to anything.

**This is not bureaucracy.** A floating `main` once shipped an agent that loaded
one platform instead of two. It came up healthy — the readiness probe only
exercises the local API server — and answered nothing, with nothing in the image
recording which plugin commit it had picked up.

## The import gate

Nothing couples the plugin to the Hermes runtime it imports from: the runtime is
pinned by digest here, the plugin moves in its own repository. A plugin commit
that reaches for a newer gateway API builds clean and boots deaf.

`scripts/check-plugin-import.sh` imports the plugin *inside the built image* —
same file, same interpreter, same uid the gateway uses — and fails if the import
raises. An import is the whole check because an import is the whole failure. The
plugin's own test suite injects a fake `gateway` package into `sys.modules`, so
it stays green against a runtime that does not have the module.

## The boot-contract gate

`scripts/check-boot-contract.sh` asserts, inside the built image, everything a
downstream variant is told below that it can rely on:

- `soul.d` parts compose in prefix order, separated by a blank line
- a literal `SOUL.md` opts out, and composition never overwrites it
- a second run changes nothing and leaves no temp file
- the composed `SOUL.md` is owned by uid 10000, mode 0600
- a `first-boot.d` hook that fails makes first boot exit non-zero
- a hook that succeeds does not, and hooks run in prefix order

Composition runs once, on a VM nobody is watching. A silent misorder ships an
agent wearing the wrong identity; a swallowed hook failure ships one that is
half configured and looks healthy.

Both gates run before the push, never against a pushed image: once a digest
exists the registry already holds the broken one.

## Soul composition

Hermes reads `$HERMES_HOME/SOUL.md` as the agent's primary identity. This image
does not ship one. It ships parts:

```
/var/lib/hermes/soul.d/00-base.md      base persona and instructions (this repo)
/var/lib/hermes/soul.d/50-persona.md   the variant's own identity (downstream)
```

On first boot, `/usr/local/lib/plow/first-boot.sh` concatenates `soul.d/*.md` in
`LC_ALL=C` order into `SOUL.md`, separated by a blank line, owned by uid 10000
and mode 0600.

Two rules make it predictable:

- **A literal `SOUL.md` wins.** An image that ships `/var/lib/hermes/SOUL.md`
  has opted out; composition never overwrites it.
- **It is idempotent.** An existing `SOUL.md` is never rewritten, so a re-run or
  a replayed boot changes nothing. Adding a `soul.d` part to a *running* agent
  does nothing — parts are a build-time contract.

Composition happens at boot rather than at build time because a downstream image
adds its parts in its own layer, and the assembled identity has to reflect
whatever the final image ended up carrying.

## Building a variant image on top of this one

A variant is a persona plus skills. It is a separate repository with a
Dockerfile whose first line is this image, and it should add nothing else:

```dockerfile
FROM public.ecr.aws/e1h7x4a2/plow-hermes-agent:base-<sha>

# Identity. 50- so it lands after 00-base.md.
COPY --chown=10000:10000 --chmod=0600 soul.d/50-persona.md /var/lib/hermes/soul.d/50-persona.md

# Skills, merged into the seed set.
COPY --chown=10000:10000 skills/ /var/lib/hermes/skills/

# First-boot work, if any. A drop-in, NOT a replacement for first-boot.sh:
# replacing that file takes soul composition with it.
COPY --chmod=0755 first-boot.d/50-variant.sh /usr/local/lib/plow/first-boot.d/50-variant.sh
```

Don't fight the init:

- **Do not start units from the setup script.** `hermes-gateway.service` has
  `Requires=exe-setup.service`; a `systemctl start hermes-gateway` from inside
  the setup script deadlocks, and the boot hangs. `WantedBy=multi-user.target`
  plus the ordering already covers it.
- **Do not write `SOUL.md` unless you mean to opt out of composition.**
- **Do not put a credential in `config.yaml`.** Tenant values stay as `${...}`
  references resolved from the env file; a provider key is named by `key_env:`.
- **Pin this image by digest or by an immutable `base-<sha>` tag**, never by a
  moving one.
- **Do not add an inbound listener.** The agent dials out; the VM accepts
  nothing.

## Publishing

Not yet automated. The image is built and gated locally with
`scripts/check-image.sh`, and pushed by hand to:

```
public.ecr.aws/e1h7x4a2/plow-hermes-agent:base-<full commit sha>
```

One immutable tag per published commit. **Publishing never blesses anything** —
`base-<sha>` is immutable and nothing deploys because it exists.

A GitHub Actions workflow that runs both gates on every pull request and
publishes from main through OIDC is written and waiting on its own reviewed pull
request. Until it lands, the gates are only as reliable as the person who
remembers to run them: run `scripts/check-image.sh` before you push an image,
every time.

## How Plow deploys

`hermes-prod` is the deploy-blessed moving tag. Plow's own deploy tooling — not
this repository — moves it, in a fixed order:

1. **Warm.** Stand up a throwaway VM on the candidate *digest*, so the first
   tenant to boot it is not the one paying for the registry's cold pull.
2. **Tag.** Move `hermes-prod` to exactly that digest by manifest, then read the
   tag back and assert it resolves to the digest that was warmed.
3. **Retire.** Remove the older warmers for that tag, and only those.

The Plow API records `hermes-prod` on an agent's claim and passes the tag
unchanged to the VM host; the API never contacts the registry itself. Rolling
back is moving the tag to an earlier digest — the `base-<sha>` tags are all
still there.

## Local development

Requires Docker with buildx and `curl`. No GitHub credential is needed.

```sh
scripts/stage-plow-chat-plugin.sh    # fetch the pinned plugin commit
scripts/check-image.sh               # build, then run both gates against it
scripts/build-image.sh my-image:dev  # build only
```

To try a different plugin commit, edit `plow-chat-plugin.ref`. There is
deliberately no environment override: an override is a way for the image and its
provenance label to disagree.

## Open

- **Licence.** This repository ships no `LICENSE` file yet. It is public;
  pick one.
