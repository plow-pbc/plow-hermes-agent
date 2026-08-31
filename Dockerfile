# Pinned by digest, not by tag. Every image built from this repository — and
# every downstream variant image built FROM it — inherits this exact upstream
# filesystem, and a moved upstream tag would substitute code on boxes holding
# customer credentials. The digest below is what `v2026.8.18` resolved to.
FROM nousresearch/hermes-agent@sha256:22e37bb4ed1b0f50cb6bd991dca7ecacd6c9f29df9b4a20fc989d32bc763ccf6 AS base

# The plow_chat plugin's canonical home is plow-pbc/hermes-plow-chat; this
# repository vendors no copy, it pins one commit. Moving the plugin is a
# one-line change to the default below. The repository is public, so the fetch
# needs no credential.
ARG PLOW_CHAT_PLUGIN_SHA=dba5fd0b38eb2a72caebbcbe427cbbbe5ea0b491

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

ENV DEBIAN_FRONTEND=noninteractive

# The container base has no init; the host boots the image's Cmd as PID 1.
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends systemd systemd-sysv sudo tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && test -x /sbin/init

# uid/gid 10000 (hermes) already exists in this base.
COPY image/seed/ /var/lib/hermes/

ARG PLOW_REVISION
LABEL org.opencontainers.image.revision="${PLOW_REVISION}"
LABEL org.opencontainers.image.source="https://github.com/plow-pbc/plow-hermes-agent"

# The label is fed from the same ARG the fetch used, so the plugin in the image
# and the plugin named by the image cannot disagree.
ARG PLOW_CHAT_PLUGIN_SHA
LABEL co.plow.plow-chat-plugin.revision="${PLOW_CHAT_PLUGIN_SHA}"
COPY --from=plugin /staged/plow_chat/ /var/lib/hermes/plugins/plow_chat/

# SOUL.md is the agent's identity and 0600 is part of the contract: nothing else
# on the box reads it.
RUN chown -R 10000:10000 /var/lib/hermes \
 && chmod 700 /var/lib/hermes \
 && chmod 600 /var/lib/hermes/SOUL.md \
 && install -d -m 0755 /usr/local/lib/plow /usr/local/lib/plow/first-boot.d

# Provisioning runs this when it exists; it runs whatever a downstream image
# dropped into first-boot.d.
COPY --chmod=0755 image/first-boot.sh /usr/local/lib/plow/first-boot.sh

COPY image/systemd/agent-setup.service image/systemd/hermes-gateway.service /etc/systemd/system/

# An unpacked image has no kernel modules to load. Masking this at build time
# keeps the static host policy out of every tenant's first-boot script.
RUN systemctl enable agent-setup.service hermes-gateway.service \
 && systemctl mask systemd-modules-load.service

ENTRYPOINT []
CMD ["/sbin/init"]
