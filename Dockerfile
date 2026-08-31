# Pinned by digest, not by tag. Every image built from this repository — and
# every downstream variant image built FROM it — inherits this exact upstream
# filesystem, and a moved upstream tag would substitute code on boxes holding
# customer credentials. The digest below is what `v2026.8.18` resolved to.
FROM nousresearch/hermes-agent@sha256:22e37bb4ed1b0f50cb6bd991dca7ecacd6c9f29df9b4a20fc989d32bc763ccf6

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

# Staged from plow-pbc/hermes-plow-chat at the commit named by
# plow-chat-plugin.ref; the label is how a running VM's image can be traced
# back to the plugin commit it carries.
ARG PLOW_CHAT_PLUGIN_SHA
LABEL co.plow.plow-chat-plugin.revision="${PLOW_CHAT_PLUGIN_SHA}"
COPY plugins/plow_chat/ /var/lib/hermes/plugins/plow_chat/

RUN chown -R 10000:10000 /var/lib/hermes \
 && chmod 700 /var/lib/hermes \
 && install -d -m 0755 /usr/local/lib/plow /usr/local/lib/plow/first-boot.d

# Provisioning runs this when it exists; it composes soul.d into SOUL.md and
# then runs whatever a downstream image dropped into first-boot.d.
COPY --chmod=0755 image/first-boot.sh /usr/local/lib/plow/first-boot.sh

COPY image/systemd/exe-setup.service image/systemd/hermes-gateway.service /etc/systemd/system/

# An unpacked image has no kernel modules to load. Masking this at build time
# keeps the static host policy out of every tenant's first-boot script.
RUN systemctl enable exe-setup.service hermes-gateway.service \
 && systemctl mask systemd-modules-load.service

ENTRYPOINT []
CMD ["/sbin/init"]
