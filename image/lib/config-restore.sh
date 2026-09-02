# Put the image's config.yaml back, atomically.
#
# One implementation because there are two callers -- the cont-init that runs
# before the runtime meets the file, and first boot -- and a recovery path that
# differs between them is a recovery path nobody has tested twice.
#
# The copy is staged beside the target and renamed onto it, so the real name
# never refers to a half-written file: an interrupted restore leaves either the
# old shape or the finished one, and the next boot's guard -- is this a regular
# file that is not a symlink -- gives the right answer about both. The `rm`
# before the rename is not redundant: `mv` onto a directory fails, and a
# directory is one of the shapes being replaced.
#
# Root writes it, then hands it over. The agent owns config.yaml on purpose --
# the chat plugin rewrites it on every connect -- so the chown is part of the
# restore rather than something a later step remembers to do.

# The one thing the file has to contain. A config that is a regular file and
# not a symlink can still be empty, truncated, or forty bytes of nothing --
# every guard above says yes and the gateway boots with no platform and no
# provider, which is the same silent outcome as the shapes, reached by looking
# harmless instead. `platforms.plow_chat` is the image's contract and the one
# block nothing else writes: model and providers are the operator's to change
# (see "Change inference provider"), the home-channel block under this one is
# the plugin's, and both survive a rewrite. Its absence means the file is not
# this image's config, whatever it looks like.
plow_config_is_ours() {
  sed -n '/^platforms:$/,/^[^ ]/p' "$1" 2>/dev/null | grep -q '^  plow_chat:$'
}

# Publish <file> as config.yaml. The one way this file is ever replaced, by
# restoration and by the boot-time edit alike -- two publication seams for one
# file is how they drift, and the second one truncated the live inode.
#
# The staging copy is a sibling because a rename only works within a filesystem,
# and the mode and owner are set on it before the rename rather than on the
# target after: at no point does the real name refer to a file that is
# half-written, or momentarily root-owned. `rm` first because a rename onto a
# directory fails, and a directory is one of the shapes being replaced.
plow_publish_config() {
  plow_cfg=/var/lib/hermes/config.yaml
  plow_cfg_tmp="$plow_cfg.plow-staged"
  rm -rf -- "$plow_cfg_tmp"
  cp "$1" "$plow_cfg_tmp"
  chmod 0640 "$plow_cfg_tmp"
  chown hermes:hermes "$plow_cfg_tmp"
  rm -rf -- "$plow_cfg"
  mv -f "$plow_cfg_tmp" "$plow_cfg"
  unset plow_cfg plow_cfg_tmp
}

plow_restore_config() {
  plow_cfg=/var/lib/hermes/config.yaml
  if [ -L "$plow_cfg" ] || [ ! -f "$plow_cfg" ] || ! plow_config_is_ours "$plow_cfg"; then
    echo "${1:-plow}: $plow_cfg is not this image's config -- restoring it" >&2
    plow_publish_config /opt/hermes/plow-seed/config.yaml
  fi
  unset plow_cfg
}
