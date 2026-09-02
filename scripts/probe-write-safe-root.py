"""Check the gateway unit's environment against the runtime's write guard.

Run by check-image.sh inside the built image. The image's own environment is
what systemd inherits and hands to the units it starts, so the process
environment here plus the unit's Environment= lines is what the gateway sees.
The guard reads HERMES_WRITE_SAFE_ROOT out of exactly that.
"""

import os
import sys

UNIT = "/etc/systemd/system/hermes-gateway.service"

for line in open(UNIT):
    line = line.strip()
    if line.startswith("Environment=") and "=" in line[len("Environment=") :]:
        key, _, value = line[len("Environment=") :].partition("=")
        os.environ[key] = value

sys.path.insert(0, "/opt/hermes")
from agent.file_safety import get_write_denied_error  # noqa: E402

home = os.environ["HERMES_HOME"]

inside = os.path.join(home, "ld", "config.json")
denial = get_write_denied_error(inside)
assert denial is None, denial

# The guard is still a guard: aligning it with the home must not read as
# unsetting it.
outside = "/opt/hermes/gateway/run.py"
assert get_write_denied_error(outside) is not None, f"{outside} is writable"

print("WRITE_SAFE_ROOT_OK")
