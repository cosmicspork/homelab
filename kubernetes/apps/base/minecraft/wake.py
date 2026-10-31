"""A page that shows whether the Hearth & Hollow server is up and wakes it.

It talks to mc-router the way a game client does, so it needs no Kubernetes
access: a status ping reads the server list entry, and a login attempt makes
mc-router scale the StatefulSet up, kick the "player" with its wake message
and arm its idle timer, as it would for a real player who never comes back.
"""

import html
import json
import os
import socket
import struct
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROUTER_HOST = os.environ["ROUTER_HOST"]
ROUTER_PORT = int(os.environ.get("ROUTER_PORT", "25565"))
# The hostname players connect to: mc-router routes on it.
SERVER_ADDRESS = os.environ["SERVER_ADDRESS"]
# mc-router's reply while the server is down; the same ConfigMap value it uses.
ASLEEP_MOTD = os.environ["AUTO_SCALE_ASLEEP_MOTD"]
# 767 is 1.21 and 1.21.1. mc-router only reads the handshake, so a mismatch
# matters only when the ping reaches the real server, which answers anyway.
PROTOCOL = int(os.environ.get("PROTOCOL_VERSION", "767"))
WAKE_NAME = "WakePage"
TIMEOUT = 10


def varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def string(s):
    data = s.encode()
    return varint(len(data)) + data


def packet(packet_id, payload=b""):
    body = varint(packet_id) + payload
    return varint(len(body)) + body


def read_varint(sock):
    value = 0
    for shift in range(0, 35, 7):
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("connection closed mid-packet")
        value |= (byte[0] & 0x7F) << shift
        if not byte[0] & 0x80:
            return value
    raise ValueError("varint too long")


def read_exact(sock, n):
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("connection closed mid-packet")
        data += chunk
    return bytes(data)


def handshake(next_state):
    return packet(
        0x00,
        varint(PROTOCOL)
        + string(SERVER_ADDRESS)
        + struct.pack(">H", ROUTER_PORT)
        + varint(next_state),
    )


def status():
    """The server list entry, as a dict, or None when the router is unreachable."""
    try:
        with socket.create_connection((ROUTER_HOST, ROUTER_PORT), TIMEOUT) as sock:
            sock.settimeout(TIMEOUT)
            sock.sendall(handshake(1) + packet(0x00))
            read_varint(sock)  # packet length
            read_varint(sock)  # packet id
            return json.loads(read_exact(sock, read_varint(sock)))
    except (OSError, ValueError):
        return None


def wake():
    """Start a login so mc-router wakes the server; it kicks us straight away."""
    name_uuid = uuid.uuid3(uuid.NAMESPACE_OID, WAKE_NAME).bytes
    with socket.create_connection((ROUTER_HOST, ROUTER_PORT), TIMEOUT) as sock:
        sock.settimeout(TIMEOUT)
        sock.sendall(handshake(2) + packet(0x00, string(WAKE_NAME) + name_uuid))
        try:
            sock.recv(4096)
        except OSError:
            pass


def motd_text(description):
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        text = description.get("text", "")
        return text + "".join(motd_text(part) for part in description.get("extra", []))
    if isinstance(description, list):
        return "".join(motd_text(part) for part in description)
    return ""


def is_online(info):
    # While the server is down (asleep or still starting), mc-router answers
    # itself with its asleep MOTD, so that message is the only reliable tell.
    return info is not None and motd_text(info.get("description")) != ASLEEP_MOTD


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hearth &amp; Hollow</title>
<style>
  :root {{ color-scheme: light dark; --bg: #f6f1e7; --fg: #2b2620; --muted: #6b6155; --accent: #b5562b; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg: #1d1a16; --fg: #eee6d8; --muted: #a89c8c; --accent: #e08454; }} }}
  body {{ margin: 0; background: var(--bg); color: var(--fg); font: 17px/1.5 system-ui, sans-serif; }}
  main {{ max-width: 32rem; margin: 0 auto; padding: 3rem 1rem; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 1.5rem; }}
  .state {{ font-size: 1.25rem; font-weight: 600; }}
  .detail, .hint {{ color: var(--muted); }}
  button {{ font: inherit; font-weight: 600; padding: .6rem 1.4rem; border: 0; border-radius: .4rem;
           background: var(--accent); color: #fff; cursor: pointer; margin-top: 1rem; }}
  code {{ font-size: .95em; }}
</style>
</head>
<body>
<main>
  <h1>Hearth &amp; Hollow</h1>
  <p class="state">{state}</p>
  <p class="detail">{detail}</p>
  {action}
  <p class="hint">Join at <code>{address}</code>. The server sleeps after an hour with nobody on.</p>
</main>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.respond(200, "ok", "text/plain")
            return
        if self.path not in ("/", "/?woke=1"):
            self.respond(404, "not found", "text/plain")
            return
        info = status()
        if info is None:
            state, detail = "Can't reach the server.", "The router isn't answering. Try again in a minute."
            action = ""
        elif is_online(info):
            players = info.get("players", {})
            state = "Awake."
            detail = html.escape(f"{players.get('online', 0)} of {players.get('max', '?')} players on. {motd_text(info.get('description'))}")
            action = ""
        else:
            state = "Asleep, or waking up."
            detail = (
                "Started it? It takes about five minutes from asleep, so launch the game now."
                if self.path == "/?woke=1"
                else "Wake it before you launch the game, and it should be ready by the time you reach the server list."
            )
            action = '<form method="post" action="/wake"><button>Wake the server</button></form>'
        self.respond(200, PAGE.format(state=state, detail=detail, action=action, address=html.escape(SERVER_ADDRESS)))

    def do_POST(self):
        if self.path != "/wake":
            self.respond(404, "not found", "text/plain")
            return
        try:
            wake()
        except OSError as err:
            self.log_error("wake failed: %s", err)
        self.send_response(303)
        self.send_header("Location", "/?woke=1")
        self.end_headers()

    def respond(self, code, body, content_type="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
