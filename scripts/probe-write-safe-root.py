"""Check the image's own environment against the runtime's write guard.

Run by check-image.sh inside the built image. HERMES_HOME and
HERMES_WRITE_SAFE_ROOT are image ENV, so this process sees exactly what the
supervised gateway sees: s6's `with-contenv` hands the service the container
environment, and the gateway's run script overrides neither of them.

The guard is the thing that used to drift. Moving the home without moving the
guard left it pointed at a directory the agent never uses, and every write into
its own home came back denied — with nothing at boot to say so.
"""

import os
import sys

sys.path.insert(0, "/opt/hermes")
from agent.file_safety import get_write_denied_error  # noqa: E402

home = os.environ["HERMES_HOME"]
assert home == "/var/lib/hermes", f"HERMES_HOME is {home}"
assert os.environ["HERMES_WRITE_SAFE_ROOT"] == home, "the write guard does not follow the home"

inside = os.path.join(home, "ld", "config.json")
denial = get_write_denied_error(inside)
assert denial is None, denial

# The guard is still a guard: aligning it with the home must not read as
# unsetting it.
outside = "/opt/hermes/gateway/run.py"
assert get_write_denied_error(outside) is not None, f"{outside} is writable"

print("WRITE_SAFE_ROOT_OK")
