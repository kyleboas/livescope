"""Boats for livescope: AIS ship positions, for the scope's Boats layer (feature "boats", /api/live/boats).

Two sources, merged by MMSI:
- aisstream.io (worldwide, from volunteers' coastal receivers): a websocket, with a free API key
  (sign in at https://aisstream.io with GitHub, then API Keys). Give it as the aisstream-key credential
  (agent-secrets: `sudo secret livescope aisstream-key`, then deploy/install-service.sh) or $AISSTREAM_KEY.
- Fintraffic's Digitraffic (the Baltic Sea and around Finland, CC BY 4.0), polled once a minute. No key, so the
  layer always has something.

/api/live/boats?lat=&lon=&nm= gives {ok, t, src, v: [[mmsi, lat, lon, sog kt, cog, heading or -1, ts, name, type,
destination, nav status], ...]}, nearest first, at most MAX_OUT.
"""
import base64
import gzip
import json
import math
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.request

from adsb import credential
from scope.core import gc_nm

AISSTREAM = ("stream.aisstream.io", "/v0/stream")
DT_LOC = "https://meri.digitraffic.fi/api/ais/v1/locations"
DT_VESSELS = "https://meri.digitraffic.fi/api/ais/v1/vessels"
DT_HEADERS = {"Digitraffic-User": "livescope", "Accept-Encoding": "gzip", "User-Agent": "livescope/1"}
KEEP_S = 30 * 60           # a position older than this is dropped (moored ships report every 3 min, class B every 3)
MAX_NM = 1500
MAX_OUT = 6000
FRESH_S = 5.0              # an area's answer is reused this long


class Boats:
    def __init__(self):
        self.lock = threading.Lock()
        self.pos = {}          # mmsi -> [lat, lon, sog, cog, hdg, ts, nav]
        self.info = {}         # mmsi -> [name, type, destination]
        self.cache = {}        # area -> (monotonic, answer)
        self.src = {}          # source name -> last good time
        threading.Thread(target=self.digitraffic, daemon=True).start()
        key = credential("aisstream-key")
        if key:
            threading.Thread(target=self.aisstream, args=(key,), daemon=True).start()

    # ---------------------------------------------------------------- the store
    def put(self, mmsi, lat, lon, sog, cog, hdg, ts, nav=-1):
        if not mmsi or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            return
        sog = sog if sog is not None and 0 <= sog < 102.3 else 0
        cog = cog if cog is not None and 0 <= cog < 360 else -1
        hdg = hdg if hdg is not None and 0 <= hdg < 360 else -1
        old = self.pos.get(mmsi)
        if old and old[5] > ts:
            return
        self.pos[mmsi] = [round(lat, 5), round(lon, 5), round(sog, 1), round(cog, 1), int(hdg), round(ts, 1), nav]

    def name(self, mmsi, name=None, kind=None, dest=None):
        i = self.info.setdefault(mmsi, ["", 0, ""])
        if name and name.strip(" @"):
            i[0] = name.strip(" @")
        if kind:
            i[1] = int(kind)
        if dest and dest.strip(" @"):
            i[2] = dest.strip(" @")

    def prune(self):
        cut = time.time() - KEEP_S
        with self.lock:
            for m in [m for m, p in self.pos.items() if p[5] < cut]:
                del self.pos[m]
            if len(self.info) > 3 * len(self.pos) + 20000:
                for m in [m for m in self.info if m not in self.pos]:
                    del self.info[m]

    # ---------------------------------------------------------------- Digitraffic (Finland, no key)
    def get_json(self, url):
        with urllib.request.urlopen(urllib.request.Request(url, headers=DT_HEADERS), timeout=30) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
        return json.loads(body)

    def digitraffic(self):
        meta_t = 0
        while True:
            try:
                if time.time() - meta_t > 1800:
                    vs = self.get_json(DT_VESSELS)
                    with self.lock:
                        for v in vs:
                            self.name(v.get("mmsi"), v.get("name"), v.get("shipType"), v.get("destination"))
                    meta_t = time.time()
                d = self.get_json(DT_LOC)
                with self.lock:
                    for f in d.get("features") or []:
                        p, (lon, lat) = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or (None, None)
                        self.put(f.get("mmsi"), lat, lon, p.get("sog"), p.get("cog"), p.get("heading"),
                                 (p.get("timestampExternal") or 0) / 1000, p.get("navStat", -1))
                self.src["digitraffic"] = time.time()
            except Exception as e:  # noqa: BLE001 - try again next minute
                print(f"livescope boats: digitraffic: {e}", file=sys.stderr, flush=True)
            self.prune()
            time.sleep(60)

    # ---------------------------------------------------------------- aisstream.io (worldwide, key)
    def aisstream(self, key):
        sub = json.dumps({"APIKey": key, "BoundingBoxes": [[[-90, -180], [90, 180]]],
                          "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport",
                                                 "ShipStaticData", "StaticDataReport"]})
        wait = 5
        while True:
            t0 = time.time()
            try:
                for msg in websocket(*AISSTREAM, sub):
                    self.on_ais(msg)
                    self.src["aisstream.io"] = time.time()
            except Exception as e:  # noqa: BLE001 - reconnect
                print(f"livescope boats: aisstream.io: {e}", file=sys.stderr, flush=True)
            wait = 5 if time.time() - t0 > 300 else min(300, wait * 2)
            time.sleep(wait)

    def on_ais(self, raw):
        try:
            m = json.loads(raw)
        except ValueError:
            return
        if m.get("error"):
            raise RuntimeError(m["error"])
        kind, meta = m.get("MessageType"), m.get("MetaData") or {}
        body = (m.get("Message") or {}).get(kind) or {}
        mmsi = meta.get("MMSI") or body.get("UserID")
        if not mmsi:
            return
        with self.lock:
            if meta.get("ShipName"):
                self.name(mmsi, meta["ShipName"])
            if kind in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
                self.put(mmsi, body.get("Latitude", meta.get("latitude")), body.get("Longitude", meta.get("longitude")),
                         body.get("Sog"), body.get("Cog"), body.get("TrueHeading"), time.time(), body.get("NavigationalStatus", -1))
                if kind == "ExtendedClassBPositionReport":
                    self.name(mmsi, body.get("Name"), body.get("Type"))
            elif kind == "ShipStaticData":
                self.name(mmsi, body.get("Name"), body.get("Type"), body.get("Destination"))
            elif kind == "StaticDataReport":
                a, b = body.get("ReportA") or {}, body.get("ReportB") or {}
                self.name(mmsi, a.get("Name") if a.get("Valid") else None, b.get("ShipType") if b.get("Valid") else None)

    # ---------------------------------------------------------------- the endpoint
    def get(self, s):
        lat, lon = float(s("lat") or 0), float(s("lon") or 0)
        nm = max(5.0, min(MAX_NM, float(s("nm") or 60)))
        step = 0.1 if nm <= 30 else 0.5 if nm <= 250 else 2.0
        key = (round(lat / step), round(lon / step), round(nm / (10 * step)))
        hit = self.cache.get(key)
        if hit and time.monotonic() - hit[0] < FRESH_S:
            return hit[1]
        dlat, dlon = nm / 60, nm / (60 * max(0.05, math.cos(math.radians(lat))))
        out = []
        with self.lock:
            for m, p in self.pos.items():
                if abs(p[0] - lat) > dlat or (dlon < 180 and abs((p[1] - lon + 540) % 360 - 180) > dlon):
                    continue
                d = gc_nm(lat, lon, p[0], p[1])
                if d <= nm:
                    i = self.info.get(m) or ("", 0, "")
                    out.append((d, [m, p[0], p[1], p[2], p[3], p[4], p[5], i[0], i[1], i[2], p[6]]))
        out.sort(key=lambda x: x[0])
        now = time.time()
        ans = {"ok": True, "t": now, "src": [k for k, t in self.src.items() if now - t < 600], "v": [v for _, v in out[:MAX_OUT]]}
        self.cache[key] = (time.monotonic(), ans)
        if len(self.cache) > 64:
            for k in sorted(self.cache, key=lambda k: self.cache[k][0])[:32]:
                del self.cache[k]
        return ans


def websocket(host, path, first, timeout=60):
    """A minimal websocket client (standard library only): connect over TLS, send `first` as a text message,
    then yield every message received (text or binary, as bytes). Ends when the server closes."""
    raw = socket.create_connection((host, 443), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: livescope/1\r\n\r\n").encode())
        f = sock.makefile("rb")
        status = f.readline()
        if b" 101 " not in status:
            raise RuntimeError("websocket refused: " + status.decode(errors="replace").strip())
        while f.readline() not in (b"\r\n", b""):
            pass

        def send(op, data):
            mask, n = os.urandom(4), len(data)
            head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else struct.pack("!BH", 0xFE, n) if n < 65536 else struct.pack("!BQ", 0xFF, n))
            sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

        def read(n):
            b = f.read(n)
            if len(b) < n:
                raise EOFError("websocket closed")
            return b

        send(1, first.encode())
        parts = []
        while True:
            b0, b1 = read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", read(8))[0]
            mask = read(4) if b1 & 0x80 else None
            data = read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = b0 & 0x0F
            if op == 8:
                return
            if op == 9:
                send(10, data)
            elif op in (0, 1, 2):
                parts.append(data)
                if b0 & 0x80:
                    yield b"".join(parts)
                    parts = []
    finally:
        sock.close()
