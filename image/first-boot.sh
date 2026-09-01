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

exit 0
