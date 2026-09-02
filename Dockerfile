# Pinned by digest, not by tag. Every image built from this repository — and
# every downstream variant image built FROM it — inherits this exact upstream
# filesystem, and a moved upstream tag would substitute code on boxes holding
# customer credentials. The digest below is what `v2026.8.18` resolved to.
FROM nousresearch/hermes-agent@sha256:22e37bb4ed1b0f50cb6bd991dca7ecacd6c9f29df9b4a20fc989d32bc763ccf6 AS base

# The plow_chat plugin's canonical home is plow-pbc/hermes-plow-chat; this
# repository vendors no copy, it pins one commit. Moving the plugin is a
# one-line change to the default below. The repository is public, so the fetch
# needs no credential.
ARG PLOW_CHAT_PLUGIN_SHA=31d0f8627bb0444119e3b89d062c52175729648e

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
# lease expires. s6 supervises the gateway on both paths this image serves, so
# this is load-bearing on both.
ENV S6_SERVICES_GRACETIME=30000

# uid/gid 10000 (hermes) already exists in this base.
COPY image/seed/ /var/lib/hermes/

# The same skills again, as BUNDLED skills, and both copies are load-bearing.
#
# The baked tree is where this image's gateway looks, because HERMES_HOME *is*
# /var/lib/hermes; the bundled copy does not replace it. What the bundled one
# covers is a home the image did not populate -- a volume or bind mount over
# HERMES_HOME hides the baked tree, and the gateway reconciles the bundled tree
# into $HERMES_HOME/skills on every boot: updating a copy the agent has not
# touched, leaving a customised one alone, never re-adding one it deleted.
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
# would register the platform twice. /opt/hermes is image-owned and survives a
# mount over the home, where a home-seeded plugin would not, and bundled is the
# placement Platform._missing_() already accepts for Platform("plow_chat") at
# import time.
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
# so root-owned SOUL.md and config.yaml sit in a writable directory and are
# still unreplaceable. Without it, file modes alone are not enough: an agent
# that cannot WRITE SOUL.md can still delete it and write its own in its place,
# because unlink permission comes from the directory, not the file. That is the
# hole this closes (plow-pbc/plow#1564).
#
# skills/ gets the same treatment for the same reason. The gateway writes there
# on every boot -- it materializes the bundled skill categories and a manifest --
# so it cannot be read-only, but a variant's own skills are copied in root-owned
# and must survive a turn. Sticky gives both: the gateway creates and removes
# what it created, and cannot rename a baked skill out of the scan path.
#
# setgid keeps new entries in the hermes group, so the gateway's own files stay
# group-readable to it however they are created.
RUN chown -R 10000:10000 /var/lib/hermes \
 && chown root:hermes /var/lib/hermes /var/lib/hermes/skills \
 && chmod 3770 /var/lib/hermes /var/lib/hermes/skills \
 && chown root:root /var/lib/hermes/SOUL.md /var/lib/hermes/config.yaml \
 && chmod 0644 /var/lib/hermes/SOUL.md /var/lib/hermes/config.yaml \
 && install -d -m 0755 /usr/local/lib/plow /usr/local/lib/plow/first-boot.d

# Provisioning runs this when it exists; it runs whatever a downstream image
# dropped into first-boot.d.
COPY --chmod=0755 image/first-boot.sh /usr/local/lib/plow/first-boot.sh
COPY --chmod=0644 image/lib/dotenv.sh /usr/local/lib/plow/dotenv.sh

# Restarting the gateway is now a supervisor signal, not a unit command, and
# the caller lives in another repository: `systemctl` is a shim over
# plow-restart-gateway for exactly the one command line plow.git sends, and it
# goes away when that caller moves.
COPY --chmod=0755 image/bin/plow-restart-gateway image/bin/systemctl /usr/local/bin/

# The local path's credential tool. Nothing on the cloud path runs it: a tenant
# is credentialled by the setup script before the gateway ever starts.
COPY --chmod=0755 image/bin/plow-activate /usr/local/bin/plow-activate

# The boot layer. s6-overlay is already in the upstream image — /init, the
# supervision tree and the s6-rc database are all there — so this adds two
# service definitions to it and installs no init of its own:
#
#   plow-init       oneshot, runs the host drop-in and first-boot.sh as root
#   hermes-gateway  longrun, the gateway as uid 10000, depends on plow-init
#
# COPY merges into the upstream tree, so the base image's own `user` bundle
# entries survive alongside ours.
COPY image/s6-overlay/ /etc/s6-overlay/

# One gateway, and one supervisor that owns it.
#
# The upstream image supports several agent "profiles" in one container: a
# cont-init hook registers a service per profile under /run/service/ and
# auto-starts any whose last recorded state was running. On a first boot there
# is no recorded state and nothing starts, so the collision is invisible — but
# the second boot starts `gateway-default`, which runs `hermes gateway run
# --replace` against the same home, and `--replace` is precisely what makes it
# kill the supervised gateway this image ships. Measured: after one
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
