"""Compare the live config.yaml with the seed the image ships, by setting.

Read through the YAML parser rather than as text. The runtime rewrites this
file through its own writer on the way up -- comments go, quoting changes,
`off` becomes 'off' -- so a text comparison reports a config that is
semantically the seed as drift, and reports it every boot. Comments are not the
contract; the keys and their values are.

Three settings are excluded because something writes them on purpose, and each
is asserted on its own elsewhere: `model.provider` and `model.default` are what
plow-config exists to write, `mcp_servers.plow.enabled` is the relay flag init
derives from the tenant's identity, and `_config_version` is the runtime's own
migration marker -- an image that pinned it would fail the first time upstream
migrated the shape.

Prints one DRIFT line per differing setting, then a sentinel. The sentinel is
the point: an empty diff and a probe that died look identical without it.
"""

import sys

import yaml

SEED = "/opt/hermes/plow-seed/config.yaml"
LIVE = "/var/lib/hermes/config.yaml"

WRITTEN_ON_PURPOSE = (
    ("_config_version",),
    ("model", "provider"),
    ("model", "default"),
    ("mcp_servers", "plow", "enabled"),
)


def drop(config: object, path: tuple[str, ...]) -> None:
    for key in path[:-1]:
        config = config.get(key) if isinstance(config, dict) else None
    if isinstance(config, dict):
        config.pop(path[-1], None)


def flatten(node: object, prefix: str = "") -> dict[str, object]:
    """One entry per leaf -- and one for an empty container, which has none.

    Without that last part an added `injected: {}` contributes no keys at all
    and the comparison sees nothing, which is the shape a probe is least likely
    to be tested against and most likely to meet.
    """
    if isinstance(node, (dict, list)):
        pairs = node.items() if isinstance(node, dict) else enumerate(node)
        flat: dict[str, object] = {}
        for key, value in pairs:
            child = f"{prefix}[{key}]" if isinstance(node, list) else (f"{prefix}.{key}" if prefix else str(key))
            flat.update(flatten(value, child))
        return flat or {prefix: type(node)()}
    return {prefix: node}


def load(path: str) -> dict[str, object]:
    with open(path) as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        sys.exit(f"{path} did not parse as a mapping")
    for setting in WRITTEN_ON_PURPOSE:
        drop(config, setting)
    return flatten(config)


seed, live = load(SEED), load(LIVE)
MISSING = object()
for key in sorted(set(seed) | set(live)):
    want, got = seed.get(key, MISSING), live.get(key, MISSING)
    if want != got:
        shown = (lambda v: "<absent>" if v is MISSING else repr(v))
        print(f"DRIFT {key}: seed={shown(want)} live={shown(got)}")
print("DRIFT_PROBE_OK")
