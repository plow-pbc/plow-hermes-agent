# What `docker buildx bake` builds, and for which machines.
#
# Two architectures from one recipe: the cloud pulls linux/amd64 onto a VM,
# and a developer's Apple Silicon Mac pulls linux/arm64 and runs it natively
# rather than under emulation. The upstream Hermes image publishes both, so
# neither is a cross-build of a single-arch base.

variable "PLOW_REVISION" {
  # The commit this image was built from. It becomes the image's revision label
  # and the `base-<sha>` tag, which is the only thing that says which source a
  # published image came from -- so an unset or short value is a build error,
  # not a blank label.
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", PLOW_REVISION))
    error_message = "PLOW_REVISION must be a full 40-character git commit SHA"
  }
}

variable "REGISTRY" {
  default = "public.ecr.aws/e1h7x4a2/plow-cloud-agents"
}

target "base" {
  context    = "."
  dockerfile = "Dockerfile"
  platforms  = ["linux/amd64", "linux/arm64"]
  args = {
    PLOW_REVISION = PLOW_REVISION
  }
  # One immutable tag per commit; nothing here moves a floating tag, and
  # publishing a tag blesses nothing -- plow.git's agents.json is what makes a
  # revision live.
  tags = ["${REGISTRY}:base-${PLOW_REVISION}"]
}
