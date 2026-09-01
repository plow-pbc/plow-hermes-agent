#!/bin/sh
# Runs the first-boot.d drop-ins, in prefix order.
#
# Invoked by the provisioning setup script (agent-setup.service) when it exists
# and is executable. Downstream images extend first boot by dropping a script
# into first-boot.d rather than by replacing this file.
set -eu

LC_ALL=C
export LC_ALL

# A hook's failure is first boot's failure. agent-setup.service is a oneshot and
# hermes-gateway.service `Requires=` it, so a non-zero exit here keeps the
# gateway from ever starting. That is the point: an agent whose setup half ran
# comes up looking healthy — the readiness probe only exercises the local API
# server — and is wrong in a way nobody sees. Better a VM that visibly never
# came up than one that answers with half its configuration.
# The dotenv holds this tenant's credential and provisioning writes it before we
# run, owned by whoever wrote it. Normalize it here, once, while we are still
# root: owned by root so uid 10000 cannot rewrite the API base and redirect its
# own bearer token, group hermes 0640 so the gateway can still READ it -- both
# through systemd's EnvironmentFile and directly, which the life variant's
# register_crons.py does when it expands a delivery target from the dotenv as
# its sole source. 0600 would close the read path and break that.
#
# The home's sticky bit is what makes this hold: without it uid 10000 could
# unlink a root-owned .env and write its own in place, whatever the mode says.
if [ -f /var/lib/hermes/.env ]; then
  chown root:hermes /var/lib/hermes/.env
  chmod 0640 /var/lib/hermes/.env
fi

# config.yaml is the one file in the home the agent is SUPPOSED to rewrite, and
# the sticky bit above is what stops it. The plow_chat plugin persists its
# home-channel block through Hermes' config writer, which writes a temp file and
# renames it over config.yaml -- and under a sticky directory uid 10000 may not
# rename over, or unlink, a file it does not own. Left root-owned it fails every
# boot with
#
#   plow_chat error: [Errno 1] Operation not permitted: '/var/lib/hermes/.config_*.tmp'
#
# Observed on both variants in prod on 2026-09-01 (agents 97e0e3db, 2ec4c001).
# It is not fatal today because the plugin falls back to PLOW_HOME_CHANNEL from
# the dotenv, but nothing that reads the persisted block can work.
#
# Giving it to hermes does not weaken what the hardening protects: SOUL.md and
# .env stay root-owned, so the sticky bit still refuses to let a turn replace
# either. This hands over the one file whose whole purpose is to be rewritten.
if [ -f /var/lib/hermes/config.yaml ]; then
  chown hermes:hermes /var/lib/hermes/config.yaml
fi

if [ -d /usr/local/lib/plow/first-boot.d ]; then
  for hook in /usr/local/lib/plow/first-boot.d/*.sh; do
    # An empty first-boot.d leaves the pattern unexpanded; -x rejects it.
    [ -x "$hook" ] || continue
    "$hook" || {
      status=$?
      echo "first-boot hook failed: $hook (exit $status)" >&2
      exit "$status"
    }
  done
fi

# Defensive restore, last thing before the gateway starts.
#
# One provision on 2026-09-01 (plow-agent-704c410c) came up with the home at
# 0700 root:root instead of 3770 root:hermes. At 0700 the directory is not
# traversable by uid 10000, so the agent cannot reach its own state and never
# starts. No candidate path reproduces it -- the image builds it correctly, the
# API's setup script never chmods the home, and a container running that exact
# setup end to end comes out right -- so the cause is still unknown and this
# does not pretend to fix it.
#
# What it does is make the mode self-healing at the last moment anything runs
# as root. The log line goes first and unconditionally: if the clobber recurs,
# the value it recorded is the evidence that survives, and without it a silent
# repair would erase the only trace of the bug.
#
# Idempotent by construction -- chmod and chown to the values the image already
# uses are a no-op on a healthy boot.
echo "first-boot: /var/lib/hermes before restore: $(stat -c '%a %U:%G' /var/lib/hermes)" >&2
chown root:hermes /var/lib/hermes
chmod 3770 /var/lib/hermes

exit 0
