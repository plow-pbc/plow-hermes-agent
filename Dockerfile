# Pinned by digest, not by tag. Every image built from this repository — and
# every downstream variant image built FROM it — inherits this exact upstream
# filesystem, and a moved upstream tag would substitute code on boxes holding
# customer credentials.
FROM nousresearch/hermes-agent@sha256:8f4e8677281eca188bc9d2fda90806646ba19941fce55fa8fda2d63112ff48a8 AS base

# The plow_chat plugin's canonical home is plow-pbc/hermes-plow-chat; this
# repository vendors no copy, it pins one commit. Moving the plugin is a
# one-line change to the default below. The repository is public, so the fetch
# needs no credential.
ARG PLOW_CHAT_PLUGIN_SHA=031dd884cba662fa7dd2065d3b2ce681b1fe5d41

# Fetched in its own stage off the same pinned base — curl and tar are already
# there, so this costs no extra upstream image and the fetch tooling never
# reaches the shipped filesystem.
FROM base AS plugin
ARG PLOW_CHAT_PLUGIN_SHA
RUN set -eu; \
    echo "$PLOW_CHAT_PLUGIN_SHA" | grep -Eq '^[0-9a-f]{40}$' \
      || { echo "PLOW_CHAT_PLUGIN_SHA is not a 40-character commit SHA" >&2; exit 1; }; \
    curl --fail-with-body --silent --show-error --location --retry 3 --retry-delay 2 \
      -o /tmp/plugin.tgz \
      "https://api.github.com/repos/plow-pbc/hermes-plow-chat/tarball/$PLOW_CHAT_PLUGIN_SHA"; \
    mkdir -p /staged/plow_chat; \
    top="$(tar -tzf /tmp/plugin.tgz | cut -d/ -f1 | uniq)"; \
    tar -xzf /tmp/plugin.tgz -C /staged/plow_chat --strip-components=2 "$top/plow-chat-platform"; \
    test -f /staged/plow_chat/__init__.py -a -f /staged/plow_chat/plugin.yaml

FROM base

# The agent's home, for everything in the image and not just for the gateway.
#
# The upstream image points both of these at /opt/data, and this image's seed
# has always lived somewhere else -- so anything that read the environment
# rather than being handed the path explicitly looked at an empty directory and
# found no identity, no config and no plugin. Setting them here is what makes
# `docker exec <container> hermes ...`, a variant's background job and the
# gateway all agree on one home. The write guard travels with it: pointing the
# home somewhere the guard did not follow denied the agent every write into its
# own directory.
ENV HERMES_HOME=/var/lib/hermes \
    HERMES_WRITE_SAFE_ROOT=/var/lib/hermes

# Hermes checkpoints its session and releases its compression lease on SIGTERM,
# and s6 otherwise allows a service 3s before continuing shutdown -- the
# replacement gateway then cannot append to that transcript until the orphaned
# lease expires. s6 supervises the gateway wherever this image boots, so this
# is load-bearing on the VM and under compose alike.
ENV S6_SERVICES_GRACETIME=30000

# uid/gid 10000 (hermes) already exists in this base.
COPY image/seed/ /var/lib/hermes/

# The same skills again, as BUNDLED skills, and both copies are load-bearing.
#
# The baked tree is where this image's gateway looks, because HERMES_HOME *is*
# /var/lib/hermes; the bundled copy does not replace it. What the bundled one
# covers is a home that arrives without them -- an empty volume on a first
# `compose up` -- and updates afterwards. The gateway reconciles it into
# $HERMES_HOME/skills on every boot: seeding what is not there, updating a copy
# the agent has not touched, leaving a customised one alone, and never
# re-adding one the agent deleted. That last one is a decision the runtime
# records in its manifest and honours; the bundled tree is a source, not a
# restore.
#
# NB the ownership block below root-owns only the top-level skills/ directory;
# `chown -R` leaves every category and skill under it owned by uid 10000, so a
# turn can still rename one out of the scan path. Pre-existing, tracked
# separately -- do not read the sticky bit as protecting the baked skills.
COPY image/seed/skills/ /opt/hermes/skills/

ARG PLOW_REVISION
LABEL org.opencontainers.image.revision="${PLOW_REVISION}"
LABEL org.opencontainers.image.source="https://github.com/plow-pbc/plow-hermes-agent"

# The label is fed from the same ARG the fetch used, so the plugin in the image
# and the plugin named by the image cannot disagree.
ARG PLOW_CHAT_PLUGIN_SHA
LABEL co.plow.plow-chat-plugin.revision="${PLOW_CHAT_PLUGIN_SHA}"
# Bundled, and deliberately in exactly one place -- a second copy under a home
# would register the platform twice. /opt/hermes is image-owned and outside
# every home, so the agent's phone line does not depend on the state of a
# directory the agent can write; bundled is also the placement
# Platform._missing_() already accepts for Platform("plow_chat") at import time.
COPY --from=plugin /staged/plow_chat/ /opt/hermes/plugins/plow_chat/

# The agent owns its state; it does not own its identity.
#
# Everything under the home is the gateway's to write -- it creates state.db,
# kanban.db, gateway.pid, the session and cron trees and a dozen lock files
# directly in this directory at runtime, so the directory itself has to stay
# writable by uid 10000. A plain root-owned home does not boot.
#
# The sticky bit is what separates the two. With `t` set, uid 10000 may create
# and remove its OWN entries here but cannot unlink or rename anyone else's --
# so a root-owned SOUL.md sits in a writable directory and is still
# unreplaceable. config.yaml is the agent's own -- the chat plugin rewrites it,
# and `plow-init` edits it as the agent rather than as root -- so it is not
# protected by this and is not meant to be. Without it, file modes alone are not enough: an agent
# that cannot WRITE SOUL.md can still delete it and write its own in its place,
# because unlink permission comes from the directory, not the file. That is the
# hole this closes.
#
# skills/ gets the same mode, but NOT the same protection, and the difference
# matters. `chown -R` above hands everything under skills/ to uid 10000; only
# the two directories named below are re-owned. The sticky bit stops a turn
# unlinking an entry it does not own, and inside skills/ it owns them all -- so
# a baked skill can still be renamed out of the scan path. What sticky protects
# are the root-owned files sitting directly in the home: SOUL.md, and the
# dotenv `plow-init` writes -- root:hermes 0640, readable by the agent and
# writable only by root, because it carries the loopback API key the runtime
# would otherwise persist a different one of. config.yaml is the agent's own,
# at hermes:hermes 0640, because the chat plugin rewrites it and `plow-init`
# edits it as the agent -- it is not protected here and is not meant to be. Do not read sticky as protecting the skills themselves: a skill the
# agent removes is gone, and the bundled copy at /opt/hermes/skills does not
# bring it back -- the runtime records the deletion and honours it. What that
# copy covers is a home that never had them and updates to ones the agent has
# not touched.
#
# setgid keeps new entries in the hermes group, so the gateway's own files stay
# group-readable to it however they are created.
RUN chown -R 10000:10000 /var/lib/hermes \
 && chown root:hermes /var/lib/hermes /var/lib/hermes/skills \
 && chmod 3770 /var/lib/hermes /var/lib/hermes/skills \
 && chown root:root /var/lib/hermes/SOUL.md \
 && chmod 0644 /var/lib/hermes/SOUL.md \
 && chown 10000:10000 /var/lib/hermes/config.yaml \
 && chmod 0640 /var/lib/hermes/config.yaml \
 && install -d -m 0755 /usr/local/lib/plow

# `plow-init` declares its credential file and Plow's answer as models rather
# than parsing either by hand. pydantic, python-dotenv and PyYAML are already
# in the runtime's environment at the versions its lock pins; this adds the one
# package that is not, with --no-deps so the install cannot move any of them.
RUN set -eu; \
    /opt/hermes/.venv/bin/python -c 'import pydantic, dotenv, yaml'; \
    uv pip install --python /opt/hermes/.venv/bin/python --no-deps pydantic-settings==2.14.2; \
    /opt/hermes/.venv/bin/python -c 'import pydantic_settings'
# A pristine config.yaml, out of the agent's reach: the copy cont-init seeds
# into a home that has none. The home's own copy belongs to uid 10000.
COPY --chmod=0644 image/seed/config.yaml /opt/hermes/plow-seed/config.yaml
# Ahead of upstream's own cont-init, which seeds a config of its own into a
# home that has none -- one with no chat platform and no provider in it.
COPY --chmod=0755 image/cont-init.d/ /etc/cont-init.d/

# The boot layer. s6-overlay is already in the upstream image — /init, the
# supervision tree and the s6-rc database are all there — so this adds two
# service definitions to it and installs no init of its own:
#
#   plow-init       oneshot, configures the agent from its credential
#   hermes-gateway  longrun, the gateway as uid 10000, depends on plow-init
#
# COPY merges into the upstream tree, so the base image's own `user` bundle
# entries survive alongside ours.
COPY image/s6-overlay/ /etc/s6-overlay/
RUN chmod 0755 /etc/s6-overlay/scripts/plow-init.py

# One gateway, and one supervisor that owns it.
#
# The upstream image supports several agent "profiles" in one container: a
# cont-init hook registers a service per profile under /run/service/ and
# auto-starts any whose last recorded state was running. On a first boot there
# is no recorded state and nothing starts, so the collision is invisible — but
# the second boot starts `gateway-default`, which runs `hermes gateway run
# --replace` against the same home, and `--replace` is precisely what makes it
# kill the supervised gateway this image ships: after one
# `docker restart`, hermes-gateway is down and a profile service nobody
# declared holds the port, with HOME and the working directory pointed at
# /opt/data.
#
# This image runs exactly one agent, so the multi-profile machinery has nothing
# to reconcile. Removing the hook is what keeps `hermes-gateway` the service
# that is actually running — the thing that depends on plow-init, drops to uid
# 10000, and binds the loopback port provisioning waits for.
RUN rm -f /etc/cont-init.d/02-reconcile-profiles

# A failed oneshot must be loud. Without this s6 logs the failure, brings up
# what it can and leaves PID 1 running, so a VM whose credential injection
# died looks alive from the outside. 2 makes /init exit instead.
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2

# exe.dev unpacks this image into a VM rootfs and boots its Cmd as PID 1;
# `docker run` does the same in a container. One entry point serves both.
ENTRYPOINT []
CMD ["/init"]
