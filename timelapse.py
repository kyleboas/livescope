"""Timelapses: the server records an area's traffic every few seconds, and the scope plays it back sped up.

A recording is of the view being looked at (up to MAX_ACTIVE at once, MAX_H hours each), and everyone can
watch every recording. With $LIVESCOPE_TIMELAPSE_KEY set, starting and stopping one takes that key
(key=...); without it, anyone can. Recording runs on the server, so it carries on with nobody
looking. Recordings are kept KEEP_DAYS days, and the oldest are dropped past MAX_MB on disk.

A recording is either of an area (kind "area": the live ADS-B feed, up to 250 nm, every INTERVAL_S) or of
the whole world (kind "world": OpenSky's worldwide snapshot, every 90 s with an OpenSky API client, else
every 15 min), one world recording at a time.

On disk, in livedata/timelapse/ (or $LIVESCOPE_TIMELAPSE_DIR): <id>.json (the recording's settings and
time span) and <id>/<hour>.ndjson.gz, one line per snapshot, each appended as its own gzip member:
    area:  [t, [[id, cs, lat, lon, alt, gs, hdg, vs, ground, type, reg, sq, emer], ...]]
    world: [t, ac, est] in OpenSky's compact form (see adsb.World: ac [hex, lat, lon, trk, gs, alt/100, ts, cs, gnd])

One endpoint, /api/live/timelapse?a=...:
    a=list                                           every recording, newest first (need_key: whether recording is locked)
    a=check&key=                                     is this the key
    a=start&lat=&lon=&nm=&h=&name=[&kind=world][&key=]   start recording that area (or the world) for h hours
    a=stop&id=[&key=]                                stop one early
    a=frames&id=&from=&to=&every=                    snapshots from <= t < to, every nth one
"""
import gzip
import hmac
import json
import os
import re
import shutil
import threading
import time
import uuid
import zlib

from scope.core import HERE

INTERVAL_S = 5          # one snapshot every this many seconds
MAX_ACTIVE = 4          # recordings at once (each costs one upstream request per snapshot)
MAX_H = 24
KEEP_DAYS = 30
MAX_MB = 4000
MAX_FRAMES = 2000       # snapshots per frames request (an area's)
MAX_WORLD_FRAMES = 12   # world snapshots per frames request (each is ~10,000 aircraft)


def read_members(path):
    """A file of gzip members appended one per snapshot; a last one still being written is left out."""
    try:
        raw = open(path, "rb").read()
    except OSError:
        return b""
    out = []
    while raw:
        z = zlib.decompressobj(31)
        try:
            data = z.decompress(raw)
        except zlib.error:
            break
        if not z.eof:
            break
        out.append(data)
        raw = z.unused_data
    return b"".join(out)


def _num(s, k, lo, hi, default):
    try:
        v = float(s(k, 20))
    except ValueError:
        return default
    return max(lo, min(hi, v)) if v == v else default


class Timelapse:
    def __init__(self, traffic, max_nm, world=None):
        self.traffic, self.max_nm, self.world = traffic, max_nm, world
        self.wbody, self.wframe = None, None        # the last world snapshot seen, and it as a line to store
        self.key = os.environ.get("LIVESCOPE_TIMELAPSE_KEY", "")
        self.dir = os.environ.get("LIVESCOPE_TIMELAPSE_DIR") or os.path.join(HERE, "livedata", "timelapse")
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()
        self.recs = {}
        for f in os.listdir(self.dir):
            if f.endswith(".json"):
                try:
                    r = json.load(open(os.path.join(self.dir, f), encoding="utf-8"))
                    self.recs[r["id"]] = r
                except (OSError, ValueError, KeyError):
                    continue
        self.prune()
        threading.Thread(target=self.worker, daemon=True).start()

    # ------------------------------------------------------------------ recording
    def save(self, r):
        p = os.path.join(self.dir, r["id"] + ".json")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(r, f)
        os.replace(p + ".tmp", p)

    def active(self, now=None):
        now = now or time.time()
        return [r for r in self.recs.values() if r["until"] > now]

    def worker(self):
        while True:
            t0 = time.time()
            with self.lock:
                todo = [dict(r) for r in self.active(t0)]
            for r in todo:
                try:
                    self.snap(r)
                except Exception:  # noqa: BLE001 - one bad snapshot mustn't stop the recorder
                    pass
            time.sleep(max(0.5, INTERVAL_S - (time.time() - t0)))

    def snap(self, r):
        if r.get("kind") == "world":
            body, _ = self.world.get()                  # keeps OpenSky's snapshot coming with nobody looking
            if body is not self.wbody:
                self.wbody, w = body, json.loads(body)
                self.wframe = (round(w["t"], 1), w["ac"], json.dumps([round(w["t"], 1), w["ac"], w.get("est") or []],
                                                                     separators=(",", ":")) + "\n") if w.get("ok") else None
            if not self.wframe or self.wframe[0] <= r.get("last", 0):
                return
            t, ac, line = self.wframe
        else:
            st = self.traffic.get(r["lat"], r["lon"], r["nm"])
            if not st.get("ok"):
                return
            t = round(st.get("t") or time.time(), 1)
            if t <= r.get("last", 0):
                return                                   # the area's cached state hasn't moved on
            ac = [[a["id"], a["cs"], a["lat"], a["lon"], a["alt"], a["gs"], a["hdg"], a["vs"], 1 if a["ground"] else 0,
                   a["type"], a["reg"], a["sq"], a["emer"]]
                  for a in st["aircraft"] if t - a["ts"] < 30]
            line = json.dumps([t, ac], separators=(",", ":")) + "\n"
        d = os.path.join(self.dir, r["id"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{int(t // 3600)}.ndjson.gz"), "ab") as f:
            f.write(gzip.compress(line.encode(), 6))
        with self.lock:
            rec = self.recs.get(r["id"])
            if rec:
                rec["first"] = rec.get("first") or t
                rec["last"] = t
                rec["n"] = rec.get("n", 0) + 1
                rec["peak"] = max(rec.get("peak", 0), len(ac))
                if rec["n"] % 12 == 1 or t + INTERVAL_S >= rec["until"]:
                    self.save(rec)

    def prune(self):
        now = time.time()
        with self.lock:
            old = sorted(self.recs.values(), key=lambda r: r["start"])
            drop = [r for r in old if r["until"] < now - KEEP_DAYS * 86400]
            size = sum(self.size(r) for r in old)
            for r in old:
                if size <= MAX_MB * 1e6:
                    break
                if r not in drop and r["until"] < now:
                    drop.append(r)
                    size -= self.size(r)
            for r in drop:
                self.recs.pop(r["id"], None)
                shutil.rmtree(os.path.join(self.dir, r["id"]), ignore_errors=True)
                try:
                    os.remove(os.path.join(self.dir, r["id"] + ".json"))
                except OSError:
                    pass

    def size(self, r):
        d = os.path.join(self.dir, r["id"])
        return sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d)) if os.path.isdir(d) else 0

    # ------------------------------------------------------------------ the endpoint
    def key_ok(self, k):
        if not self.key:
            return True
        if hmac.compare_digest(k.encode(), self.key.encode()):
            return True
        time.sleep(1)                                # slows down guessing
        return False

    def public(self, r, now):
        world = r.get("kind") == "world"
        return {k: r.get(k) for k in ("id", "name", "lat", "lon", "nm", "start", "until", "first", "last", "n", "peak")} | \
               {"kind": "world" if world else "area", "rec": r["until"] > now,
                "every_s": self.world.every if world else INTERVAL_S}

    def endpoint(self, s):
        a, now = s("a", 10), time.time()
        if a == "list":
            with self.lock:
                return {"recs": [self.public(r, now) for r in sorted(self.recs.values(), key=lambda r: -r["start"])],
                        "max_active": MAX_ACTIVE, "max_h": MAX_H, "need_key": bool(self.key),
                        "world_every": self.world.every if self.world else None}
        if a in ("check", "start", "stop") and not self.key_ok(s("key", 200)):
            return {"error": "wrong key", "bad_key": True}
        if a == "check":
            return {"ok": True}
        if a == "start":
            lat, lon = _num(s, "lat", -89, 89, None), _num(s, "lon", -180, 180, None)
            if lat is None or lon is None:
                return {"error": "lat and lon are needed"}
            nm, h = _num(s, "nm", 5, self.max_nm, 60), _num(s, "h", 0.1, MAX_H, 1)
            world = s("kind", 6) == "world"
            if world and not self.world:
                return {"error": "no worldwide traffic on this site"}
            name = re.sub(r"[^\w .,/()+-]", "", s("name", 40)).strip() or ("World" if world else f"{lat:.2f}, {lon:.2f}")
            self.prune()
            with self.lock:
                if len(self.active(now)) >= MAX_ACTIVE:
                    return {"error": f"{MAX_ACTIVE} timelapses are already recording; try again when one finishes"}
                if world and any(x.get("kind") == "world" for x in self.active(now)):
                    return {"error": "the world is already being recorded"}
                r = {"id": uuid.uuid4().hex[:10], "name": name, "lat": round(lat, 3), "lon": round(lon, 3), "nm": round(nm),
                     "kind": "world" if world else "area",
                     "start": now, "until": now + h * 3600, "first": None, "last": 0, "n": 0, "peak": 0}
                self.recs[r["id"]] = r
                self.save(r)
                return {"rec": self.public(r, now)}
        r = self.recs.get(s("id", 12))
        if not r:
            return {"error": "no such timelapse"}
        if a == "stop":
            with self.lock:
                if r["until"] > now:
                    r["until"] = now
                    self.save(r)
                return {"rec": self.public(r, now)}
        if a == "frames":
            t0, t1 = _num(s, "from", 0, 1e11, 0), _num(s, "to", 0, 1e11, 1e11)
            every = int(_num(s, "every", 1, 1000, 1))
            return self.frames(r, t0, t1, every)
        return {"error": "unknown action"}

    def frames(self, r, t0, t1, every):
        d, out, i, more = os.path.join(self.dir, r["id"]), [], 0, False
        hours = sorted(int(f.split(".")[0]) for f in os.listdir(d) if f.endswith(".ndjson.gz")) if os.path.isdir(d) else []
        for hr in hours:
            if (hr + 1) * 3600 <= t0 or hr * 3600 >= t1:
                continue
            for line in read_members(os.path.join(d, f"{hr}.ndjson.gz")).splitlines():
                t = float(line[1:line.index(b",")])
                if t < t0 or t >= t1:
                    continue
                if i % every == 0:
                    if len(out) >= (MAX_WORLD_FRAMES if r.get("kind") == "world" else MAX_FRAMES):
                        more = True
                        break
                    out.append(line)
                i += 1
            if more:
                break
        return b'{"frames":[' + b",".join(out) + b'],"more":' + (b"true" if more else b"false") + b"}"
