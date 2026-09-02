"""Print one top-level config.yaml section as flattened settings, for diffing.

Read through the YAML parser rather than as text: the runtime rewrites this
file through its own writer on the way up -- comments go, quoting changes -- so
two texts differ where the settings do not, and a check comparing them reports
drift on every boot.

    python - providers < probe-config-section.py

Prints `path = value` per leaf, sorted, then a sentinel. The sentinel is the
point: an identical pair of empty outputs and a probe that died look the same
without it.
"""

import sys

import yaml

LIVE = "/var/lib/hermes/config.yaml"


def flatten(node: object, prefix: str = "") -> dict[str, object]:
    """One entry per leaf, and one for an empty container, which has none."""
    if isinstance(node, (dict, list)):
        pairs = node.items() if isinstance(node, dict) else enumerate(node)
        flat: dict[str, object] = {}
        for key, value in pairs:
            child = f"{prefix}[{key}]" if isinstance(node, list) else (f"{prefix}.{key}" if prefix else str(key))
            flat.update(flatten(value, child))
        return flat or {prefix: type(node)()}
    return {prefix: node}


with open(LIVE) as handle:
    config = yaml.safe_load(handle)
if not isinstance(config, dict):
    sys.exit(f"{LIVE} did not parse as a mapping")

for section in sys.argv[1:]:
    for key, value in sorted(flatten(config.get(section), section).items()):
        print(f"{key} = {value!r}")
print("SECTION_PROBE_OK")
