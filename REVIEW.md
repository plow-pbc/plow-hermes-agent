# Review instructions — plow-hermes-agent

Repo-specific reviewer policy. The universal voice posture (Broken-Glass,
pro-simplification, and the don't-propose list) is supplied by the reviewers
themselves and is deliberately not restated here.

## What this repo is

**The base OCI image every Plow agent is built from** — boot, `plow-init`, the
gateway config seed, the base persona, and the pin of the `plow_chat` plugin.
`README.md` owns the credential contract, the variant contract, and the repo
map; this file does not restate them. Flag drift between that prose and the
code, in either direction.

**Stage:** pre-PMF, one operator, a handful of tenants booting this image as a
VM rootfs on exe.dev or as a container. Iteration speed beats hardening for
scale: prefer loud failures to fallbacks, and don't guard edge cases a fleet
this size cannot reach.

**Distribution model:** one immutable `base-<full-sha>` tag per commit,
published by CI in `plow-pbc/plow`, which also pins which tag a tenant boots.
Variants build `FROM` this image by digest, so a fix here reaches them only
through a pin bump.

## Review priority

Subtractive remedies outrank additive ones. `plow-init` is a oneshot every
service depends on, so anything it refuses starts nothing — a refusal that is
loud and specific is the design here, not a finding.

**Repo-specific contrast pairs:**

| Base-image DON'T (suppress / flag-as-shape) | Base-image DO (real finding) |
|---|---|
| — | Flag a change that a **sibling repo owns** per `plow-hermes-agent` README § The repos: the per-turn prompt framing or a Plow tool description (the `plow_chat` plugin, `hermes-plow-chat`; the base persona in `image/seed/SOUL.md` stays here), a persona or a skill for one assistant (that assistant's variant repo), a patch to the Hermes runtime (`srosro/hermes-agent`, then upstream — the base has no patch mechanism on purpose). The test is who else would have to change if the fact changed. |

**Update cadence:** edit when the stage changes. Product and architecture edits
belong in `README.md`, not here.
