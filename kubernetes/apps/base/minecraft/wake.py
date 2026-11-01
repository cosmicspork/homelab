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
from string import Template

ROUTER_HOST = os.environ["ROUTER_HOST"]
ROUTER_PORT = int(os.environ.get("ROUTER_PORT", "25565"))
# The hostname players connect to: mc-router routes on it.
SERVER_ADDRESS = os.environ["SERVER_ADDRESS"]
# mc-router's reply while the server is down; the same ConfigMap value it uses.
ASLEEP_MOTD = os.environ["AUTO_SCALE_ASLEEP_MOTD"]
# Its reply while a wake is in progress.
LOADING_MOTD = os.environ["AUTO_SCALE_LOADING_MOTD"]
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
    # While the server is down, mc-router answers itself: with its loading MOTD
    # while a wake is in progress, otherwise its asleep MOTD. Those messages
    # are the only reliable tell.
    return info is not None and motd_text(info.get("description")) not in (ASLEEP_MOTD, LOADING_MOTD)


def is_waking(info):
    return info is not None and motd_text(info.get("description")) == LOADING_MOTD


# The scene is the pack icon (hearth-and-hollow assets/icon.svg) widened to a
# banner, under the pack wordmark rendered from assets/wordmark.svg without its
# "Modpack" line. The body's class (awake, asleep, waking, unreachable) lights or darks
# the cottage window, the chimney smoke and the cave lantern.
PAGE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
$refresh
<title>$title · Hearth &amp; Hollow</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%2318203d'/%3E%3Cpath d='M2 8 8 3l6 5z' fill='%23b5523c'/%3E%3Crect x='4' y='8' width='8' height='6' fill='%23d8b48a'/%3E%3Crect x='5' y='9' width='3' height='3' fill='%23ffd27a'/%3E%3C/svg%3E">
<style>
  :root {
    --sky-top: #18203d; --sky-mid: #34305a; --sky-low: #6b4a6c;
    --slate: #25232f; --slate-hi: #2e2c3a; --slate-edge: #3b3848;
    --cream: #f4e3bf; --text: #ece3d3; --muted: #aaa0b4;
    --ember: #f2a23b; --ember-hi: #ffc861; --glow: #ffd27a;
    --awake: #8fd18a; --asleep: #b88be8; --waking: #ffc861; --down: #e8806c;
  }
  * { box-sizing: border-box; }
  html { background: var(--slate); }
  body { margin: 0; min-height: 100vh; background: var(--slate); color: var(--text);
         font: 17px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }

  header { position: relative; overflow: hidden;
           background: linear-gradient(var(--sky-top), var(--sky-mid) 60%, var(--sky-low)); }
  header h1 { position: absolute; inset: clamp(1.25rem, 4vh, 2rem) 1rem auto; margin: 0; z-index: 1; text-align: center; }
  header h1 img { width: clamp(220px, 30vw, 320px); height: auto; filter: drop-shadow(0 4px 0 rgba(10, 8, 20, .45)); }
  .scene { display: block; width: 100%; height: clamp(340px, 58vh, 520px); }

  main { max-width: 34rem; margin: 0 auto; padding: .5rem 1rem 3rem; }
  .card { background: var(--slate-hi); border: 2px solid var(--slate-edge); border-radius: 6px;
          padding: 1.25rem 1.4rem 1.4rem; box-shadow: 0 6px 0 rgba(0, 0, 0, .25); }
  .state { display: flex; align-items: center; gap: .65rem; margin: 0;
           font-size: 1.45rem; font-weight: 700; color: var(--cream); }
  .dot { width: .7rem; height: .7rem; flex: none; background: var(--dot); box-shadow: 0 0 0 4px color-mix(in srgb, var(--dot) 22%, transparent); }
  .awake .dot { --dot: var(--awake); }
  .asleep .dot { --dot: var(--asleep); }
  .waking .dot { --dot: var(--waking); animation: pulse 1.4s ease-in-out infinite; }
  .unreachable .dot { --dot: var(--down); }
  .detail { margin: .5rem 0 0; color: var(--text); }
  .motd { margin: .75rem 0 0; padding: .5rem .75rem; background: var(--slate); border-left: 3px solid var(--slate-edge);
          color: var(--muted); font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: .9rem; }

  .wake { margin: 1.25rem 0 0; }
  button { font: inherit; font-weight: 700; cursor: pointer; border: 0; border-radius: 4px; }
  .wake button { width: 100%; padding: .8rem 1.25rem; font-size: 1.05rem;
                 background: var(--ember); color: #2a1709;
                 box-shadow: inset 0 -4px 0 rgba(0, 0, 0, .22), inset 0 2px 0 rgba(255, 255, 255, .25); }
  .wake button:hover { background: var(--ember-hi); }
  .wake button:active { box-shadow: inset 0 -1px 0 rgba(0, 0, 0, .22); transform: translateY(2px); }
  .wake.again button { background: transparent; color: var(--ember-hi); box-shadow: inset 0 0 0 2px var(--slate-edge); font-size: .95rem; padding: .55rem 1rem; }
  .wake.again button:hover { box-shadow: inset 0 0 0 2px var(--ember); }
  button:focus-visible { outline: 3px solid var(--cream); outline-offset: 3px; }

  .join { margin-top: 1.75rem; }
  .label { display: block; margin-bottom: .4rem; color: var(--muted); font-size: .8rem; font-weight: 600;
           letter-spacing: .08em; text-transform: uppercase; }
  .address { display: flex; align-items: stretch; gap: .5rem; }
  .address code { flex: 1; min-width: 0; padding: .6rem .8rem; background: #1c1a24; border: 2px solid var(--slate-edge);
                  border-radius: 4px; color: var(--cream); font: 1.05rem ui-monospace, "SF Mono", Menlo, Consolas, monospace;
                  overflow-wrap: anywhere; }
  .copy { padding: 0 1rem; background: var(--slate-edge); color: var(--text); }
  .copy:hover { background: #4a4659; }
  .hint { margin: .9rem 0 0; color: var(--muted); font-size: .92rem; }

  /* The scene by state. */
  .window { fill: #3a3346; transition: fill .6s; }
  .warm, .smoke, .lantern-glow { opacity: 0; transition: opacity .8s; }
  .lantern { fill: #5a4d4a; }
  .awake .window, .waking .window { fill: var(--glow); }
  .awake .warm, .awake .smoke, .awake .lantern-glow { opacity: 1; }
  .awake .lantern { fill: var(--glow); }
  .waking .warm { opacity: .7; animation: flicker 2.2s ease-in-out infinite; }
  .waking .smoke { opacity: .35; }
  .waking .lantern { fill: #c9a25c; }
  .unreachable .scene { filter: saturate(.35) brightness(.8); }
  .star { animation: twinkle 4s ease-in-out infinite; }
  .star:nth-child(3n) { animation-delay: -1.3s; }
  .star:nth-child(3n+1) { animation-delay: -2.6s; }
  .firefly { opacity: 0; }
  .awake .firefly { animation: twinkle 3s ease-in-out infinite; }
  .awake .firefly:nth-child(2n) { animation-delay: -1.5s; }
  .smoke path { animation: drift 6s ease-in-out infinite; }

  @keyframes pulse { 50% { opacity: .35; } }
  @keyframes flicker { 0%, 100% { opacity: .75; } 40% { opacity: .35; } 55% { opacity: .9; } 70% { opacity: .5; } }
  @keyframes twinkle { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
  @keyframes drift { 50% { transform: translateX(6px); } }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; }
    .awake .firefly { opacity: 1; }
  }
</style>
</head>
<body class="$scene">
<header>
  <h1><img src="/wordmark.png" width="1376" height="608" alt="Hearth &amp; Hollow"></h1>
  <svg class="scene" viewBox="0 -140 2400 660" preserveAspectRatio="xMidYMax slice" aria-hidden="true">
    <defs>
      <radialGradient id="moonGlow"><stop offset="0" stop-color="#f6e6c3" stop-opacity=".35"/><stop offset="1" stop-color="#f6e6c3" stop-opacity="0"/></radialGradient>
      <radialGradient id="warm"><stop offset="0" stop-color="#ffc861" stop-opacity=".75"/><stop offset=".45" stop-color="#ff9f43" stop-opacity=".25"/><stop offset="1" stop-color="#ff9f43" stop-opacity="0"/></radialGradient>
      <clipPath id="caveClip"><path d="M262 432 V376 A58 58 0 0 1 378 376 V432 Z"/></clipPath>
    </defs>
    <g fill="#f6e7c8">
      <circle class="star" cx="180" cy="120" r="2.5"/><circle class="star" cx="420" cy="230" r="2"/>
      <circle class="star" cx="610" cy="90" r="2"/><circle class="star" cx="760" cy="250" r="1.8"/>
      <circle class="star" cx="880" cy="150" r="2.5"/><circle class="star" cx="1010" cy="240" r="2"/>
      <circle class="star" cx="1320" cy="180" r="1.8"/><circle class="star" cx="1500" cy="250" r="2"/>
      <circle class="star" cx="1560" cy="110" r="2.5"/><circle class="star" cx="1900" cy="230" r="2"/>
      <circle class="star" cx="2050" cy="120" r="1.8"/><circle class="star" cx="2260" cy="200" r="2.5"/>
      <circle class="star" cx="340" cy="-40" r="2"/><circle class="star" cx="1960" cy="-60" r="2.5"/>
      <circle class="star" cx="760" cy="-90" r="1.8"/><circle class="star" cx="1500" cy="20" r="2"/>
      <circle class="star" cx="300" cy="300" r="1.8"/><circle class="star" cx="2160" cy="300" r="2"/>
    </g>
    <circle cx="1680" cy="190" r="80" fill="url(#moonGlow)"/>
    <circle cx="1680" cy="190" r="34" fill="#f4e3bf"/>
    <circle cx="1668" cy="182" r="6" fill="#e6d1a6"/><circle cx="1690" cy="202" r="4" fill="#e6d1a6"/>

    <path d="M0 372 C 300 324 600 334 880 360 C 1140 326 1420 318 1680 342 C 1940 316 2180 326 2400 352 V520 H0 Z" fill="#273a4b"/>
    <g fill="#1f3040">
      <path d="M640 346 L658 298 L676 346 Z"/><path d="M668 350 L688 290 L708 350 Z"/>
      <path d="M1790 338 L1806 296 L1822 338 Z"/><path d="M2200 336 L2218 290 L2236 336 Z"/>
      <path d="M220 360 L236 318 L252 360 Z"/>
    </g>

    <g transform="translate(925 80)">
      <g class="smoke" fill="none" stroke="#d4cadb" stroke-linecap="round" opacity=".5">
        <path d="M246 150 C 236 128 262 116 252 94 C 244 76 266 62 260 40" stroke-width="10"/>
      </g>
      <rect x="236" y="156" width="18" height="40" fill="#5b3a2e"/>
      <rect x="232" y="150" width="26" height="9" rx="2" fill="#4a2f26"/>
      <rect x="166" y="198" width="98" height="80" fill="#d8b48a"/>
      <rect x="166" y="198" width="98" height="10" fill="#c39a70"/>
      <path d="M152 204 L215 146 L278 204 Z" fill="#9c3f2f"/>
      <path d="M152 204 L215 146 L222 152 L164 206 Z" fill="#b5523c"/>
      <circle class="warm" cx="200" cy="232" r="70" fill="url(#warm)"/>
      <rect class="window" x="184" y="216" width="34" height="30" rx="3"/>
      <path d="M201 216 V246 M184 231 H218" stroke="#6b3f2a" stroke-width="4"/>
      <rect x="184" y="216" width="34" height="30" rx="3" fill="none" stroke="#6b3f2a" stroke-width="4"/>
      <rect x="232" y="222" width="20" height="40" rx="3" fill="#6b3f2a"/>
      <circle cx="247" cy="244" r="2" fill="#e9b65b"/>
    </g>

    <path d="M0 430 C 420 410 820 336 1140 334 C 1480 332 1900 400 2400 430 V520 H0 Z" fill="#3e6a55"/>
    <path d="M0 430 C 420 410 820 336 1140 334 C 1480 332 1900 400 2400 430" fill="none" stroke="#69995f" stroke-width="9" stroke-linecap="round"/>
    <g fill="#2f5546">
      <path d="M840 392 L862 332 L884 392 Z"/><path d="M872 398 L890 350 L908 398 Z"/>
      <path d="M1620 400 L1642 340 L1664 400 Z"/><path d="M1652 404 L1670 356 L1688 404 Z"/>
    </g>
    <path d="M1168 342 C 1180 372 1240 384 1290 404 C 1320 416 1324 436 1328 456" fill="none" stroke="#86694b" stroke-width="9" stroke-linecap="round"/>

    <g transform="translate(1060 80)">
      <path d="M262 432 V376 A58 58 0 0 1 378 376 V432 Z" fill="#16111c"/>
      <g clip-path="url(#caveClip)">
        <circle class="lantern-glow" cx="320" cy="378" r="72" fill="url(#warm)"/>
        <rect x="266" y="392" width="12" height="12" fill="#f2a23b"/><rect x="278" y="404" width="10" height="10" fill="#f2a23b"/>
        <rect x="358" y="384" width="12" height="12" fill="#b88be8"/><rect x="346" y="398" width="10" height="10" fill="#b88be8"/>
        <rect x="300" y="414" width="11" height="11" fill="#6ad7d2"/>
      </g>
      <path d="M262 432 V376 A58 58 0 0 1 378 376 V432" fill="none" stroke="#5a5566" stroke-width="10"/>
      <path d="M320 318 V354" stroke="#2b2028" stroke-width="3"/>
      <rect x="311" y="352" width="18" height="6" rx="2" fill="#2b2028"/>
      <rect class="lantern" x="312" y="358" width="16" height="20" rx="3"/>
      <rect x="311" y="377" width="18" height="5" rx="2" fill="#2b2028"/>
    </g>

    <g fill="#ffe08a">
      <circle class="firefly" cx="980" cy="402" r="3"/><circle class="firefly" cx="1060" cy="380" r="2.5"/>
      <circle class="firefly" cx="1480" cy="390" r="3"/><circle class="firefly" cx="760" cy="430" r="2.5"/>
      <circle class="firefly" cx="1560" cy="452" r="2.5"/><circle class="firefly" cx="420" cy="448" r="2.5"/>
      <circle class="firefly" cx="1900" cy="440" r="3"/>
    </g>

    <path d="M0 504 C 600 488 1800 488 2400 504 V520 H0 Z" fill="#2e2c3a"/>
    <path d="M0 514 C 600 506 1800 506 2400 514 V520 H0 Z" fill="#25232f"/>
  </svg>
</header>
<main>
  <section class="card" aria-live="polite">
    <p class="state"><span class="dot" aria-hidden="true"></span>$state</p>
    $detail
    $motd
    $action
  </section>
  <section class="join">
    <span class="label">Server address</span>
    <div class="address">
      <code id="address">$address</code>
      <button type="button" class="copy" hidden>Copy</button>
    </div>
    <p class="hint">The server sleeps after an hour with nobody on. Joining while it's asleep wakes it too, but you'll have to try again once it's up.</p>
  </section>
</main>
<script>
  const copy = document.querySelector(".copy");
  if (navigator.clipboard) {
    copy.hidden = false;
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(document.getElementById("address").textContent);
      copy.textContent = "Copied";
      setTimeout(() => { copy.textContent = "Copy"; }, 2000);
    });
  }
</script>
</body>
</html>
""")

with open(os.path.join(os.path.dirname(__file__), "wordmark.png"), "rb") as f:
    WORDMARK = f.read()

WAKE_BUTTON = '<form class="wake" method="post" action="/wake"><button>Wake the server</button></form>'
WAKE_AGAIN = '<form class="wake again" method="post" action="/wake"><button>Not waking? Send another wake</button></form>'
# Seconds between reloads while the server boots.
WAKING_REFRESH = 15


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.respond(200, "ok", "text/plain")
            return
        if self.path == "/wordmark.png":
            self.respond(200, WORDMARK, "image/png", "max-age=86400")
            return
        if self.path not in ("/", "/?woke=1"):
            self.respond(404, "not found", "text/plain")
            return
        info = status()
        page = {"refresh": "", "detail": "", "motd": "", "action": ""}
        if info is None:
            page.update(
                scene="unreachable",
                title="Can't reach the server",
                state="Can't reach the server",
                detail="<p class=\"detail\">The router isn't answering. Try again in a minute.</p>",
            )
        elif is_online(info):
            players = info.get("players", {})
            names = [p.get("name", "") for p in players.get("sample") or []]
            detail = f"{players.get('online', 0)} of {players.get('max', '?')} players on"
            detail += f": {', '.join(names)}." if names else "."
            motd = motd_text(info.get("description")).strip()
            page.update(
                scene="awake",
                title="Awake",
                state="Awake",
                detail=f'<p class="detail">{html.escape(detail)}</p>',
                motd=f'<p class="motd">{html.escape(motd)}</p>' if motd else "",
            )
        elif is_waking(info) or self.path == "/?woke=1":
            page.update(
                scene="waking",
                title="Waking up",
                state="Waking up",
                detail=f'<p class="detail">It takes about five minutes from asleep. This page checks again every {WAKING_REFRESH} seconds.</p>',
                refresh=f'<meta http-equiv="refresh" content="{WAKING_REFRESH}">',
                action=WAKE_AGAIN,
            )
        else:
            page.update(
                scene="asleep",
                title="Asleep",
                state="Asleep",
                action=WAKE_BUTTON,
            )
        self.respond(200, PAGE.substitute(page, address=html.escape(SERVER_ADDRESS)))

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

    def respond(self, code, body, content_type="text/html; charset=utf-8", cache="no-store"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
