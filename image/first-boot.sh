#!/bin/sh
# Runs the first-boot.d drop-ins, in prefix order.
#
# Invoked by the `plow-init` oneshot, and — on the cloud path — a second time
# by the VM host's own setup script, which ends by calling this file. Every
# step below is idempotent so the two calls collapse into one outcome.
# Downstream images extend first boot by dropping a script into first-boot.d
# rather than by replacing this file.
set -eu

. /usr/local/lib/plow/root-path.sh

LC_ALL=C
export LC_ALL

# A hook's failure is first boot's failure. `plow-init` is an s6 oneshot and
# every service declares it as a dependency, so a non-zero exit here keeps the
# gateway from ever starting. That is the point: an agent whose setup half ran
# comes up looking healthy — the readiness probe only exercises the local API
# server — and is wrong in a way nobody sees. Better a VM that visibly never
# came up than one that answers with half its configuration.
# The dotenv holds this tenant's credential and provisioning writes it before we
# run, owned by whoever wrote it. Normalize it here, once, while we are still
# root: owned by root so uid 10000 cannot rewrite the API base and redirect its
# own bearer token, group hermes 0640 so the gateway can still READ it -- both
# when its service script sources the file and directly, which the life
# variant's register_crons.py does when it expands a delivery target from the
# dotenv as its sole source. 0600 would close the read path and break that.
#
# The home's sticky bit is what makes this hold: without it uid 10000 could
# unlink a root-owned .env and write its own in place, whatever the mode says.
if [ -f /var/lib/hermes/.env ]; then
  chown root:hermes /var/lib/hermes/.env
  chmod 0640 /var/lib/hermes/.env
fi

# SOUL.md is the opposite case: the identity the agent answers as, and the one
# file a compromised turn would most want to rewrite. The image builds it
# root-owned; this re-asserts that on every boot so a variant layer that copied
# its own in under uid 10000 does not silently hand the agent its own identity.
# The mode is left alone -- a variant may legitimately choose one.
if [ -f /var/lib/hermes/SOUL.md ]; then
  chown root:root /var/lib/hermes/SOUL.md
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
# ...and handing it over is what makes the next lines necessary. An agent that
# owns a file in a sticky directory may unlink it -- so it may delete this one
# outright, or replace it with a symlink -- and then `[ -f ]` is true of the TARGET, `chown` follows
# it, and the config writer below writes through it. Root gives away the
# ownership of an arbitrary file and overwrites its contents. Reproduced: a
# symlink to a root-owned file came back hermes-owned with a `model:` block
# appended to it.
#
# So the file is restored rather than repaired. `rm` on a symlink removes the
# link, never its target, and the copy comes from outside the home, where no
# turn can reach it.
if [ -L /var/lib/hermes/config.yaml ] || [ ! -f /var/lib/hermes/config.yaml ]; then
  echo "first-boot: /var/lib/hermes/config.yaml is not a regular file -- restoring the image's copy" >&2
  # -r as well as -f: a directory is one of the shapes this catches, an absent
  # file is another, and neither flag follows a symlink.
  rm -rf /var/lib/hermes/config.yaml
  cp /opt/hermes/plow-seed/config.yaml /var/lib/hermes/config.yaml
fi

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

# Defensive restore of the sticky+setgid pair, last thing before any service
# starts, and the reason this runs on every boot rather than only the first.
#
# Two different things reach for this directory, and both leave it at 0700.
#
# The Hermes runtime bootstraps the home it is pointed at: finding one it does
# not own, it takes it and its seeded subdirectories, leaving both 0700
# hermes:hermes. That is right for a plain data volume and wrong for this one,
# where root ownership plus the sticky bit is the whole of the plow#1564
# hardening -- at 0700 hermes:hermes the agent owns the directory, and owning
# the directory is what lets it unlink a root-owned SOUL.md whatever the file's
# mode says. This runs after that bootstrap, which is what makes it a repair.
#
# The second is `secure_parent_dir` (hermes_constants.py): every write of the
# auth store chmods its parent -- this directory -- to 0700, and swallows the
# failure. As uid 10000 it fails and nothing happens. As ROOT it succeeds, and
# the home lands at 0700 root:hermes: the group bit gone, so the agent cannot
# traverse its own home and every turn EPERMs. That is the state seen in prod
# on plow-agent-704c410c (2026-09-01) and the one a root probe produced in
# `exe.py`'s own history. Nothing in this image runs the runtime as root --
# every service drops first, and upstream's /opt/hermes/bin/hermes shim drops a
# root `docker exec` -- so the reachable door is a root process invoking the
# venv binary or hermes_cli directly, past the shim.
#
# Hence the log line first and unconditionally: if a mode arrives that neither
# of those explains, the value it recorded is the evidence that survives, and a
# silent repair would erase the only trace.
#
# Idempotent by construction -- chown and chmod to the values the image already
# uses are a no-op on a boot where nothing touched them.
echo "first-boot: /var/lib/hermes before restore: $(stat -c '%a %U:%G' /var/lib/hermes)" >&2
echo "first-boot: /var/lib/hermes/skills before restore: $(stat -c '%a %U:%G' /var/lib/hermes/skills)" >&2
chown root:hermes /var/lib/hermes /var/lib/hermes/skills
chmod 3770 /var/lib/hermes /var/lib/hermes/skills

exit 0
