"""A stand-in for Plow's `GET /v1/agents/cloud/me`, for check-image.sh.

The image asks Plow who it is with the credential a host dropped in, and the
answer decides the home channel and whether the relay is on. Only a server can
put that path under test, so this is one: it maps a bearer token to an answer,
which is what lets a rotation be checked -- the same VM, a new token in the
same file, a different identity coming back.

Run inside the image itself (its python, its network), never shipped in it.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

RELAY = "http://stub:8080/v1/relay/devices/dev_cloud_check/mcp"

# token -> what Plow says about the agent holding it. `mcp_url: None` is the
# tenant with no relay, which is a supported shape and not an error.
IDENTITIES = {
    "cloud-token-one": {"chat_uid": "cht_cloud_one", "mcp_url": RELAY},
    "cloud-token-two": {"chat_uid": "cht_cloud_two", "mcp_url": RELAY},
    "cloud-token-norelay": {"chat_uid": "cht_cloud_three", "mcp_url": None},
}


# Control files, touched with `docker exec` on the running stub, so a check can
# change what Plow says WITHOUT changing the credential the agent holds. That
# separation is the whole point: the interesting cases are the ones where the
# token is unchanged and the answer is not.
REVOKED = "/revoked"          # 404: this credential is not this agent's any more
UNAVAILABLE = "/unavailable"  # 503: Plow is having a bad day
GARBAGE = "/garbage"          # 200, and not an identity


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's spelling
        if self.path != "/v1/agents/cloud/me":
            self.send_error(404)
            return
        if os.path.exists(UNAVAILABLE):
            self.send_error(503)
            return
        if os.path.exists(REVOKED):
            self.send_error(404)
            return
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        identity = IDENTITIES.get(token)
        if identity is None:
            self.send_error(401)
            return
        if os.path.exists(GARBAGE):
            identity = {"chat_uid": "", "mcp_url": None}
        body = json.dumps(identity).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
