"""Real-world traffic for livescope: public ADS-B aggregators (readsb JSON, at most 250 nm around a
point; adsb.lol, then adsb.fi), cached per area so everyone looking at the same place shares one
upstream request; OpenSky's worldwide snapshot with ocean estimates (along the day's North Atlantic Tracks
where they fit); and adsbdb.com callsign routes.
(ATIS and METARs come from scope.weather.)
"""
import calendar
import gzip
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from scope.core import R_NM, gc_brg, gc_dest, gc_nm

SOURCES = (("adsb.lol", "https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}"),
           ("adsb.fi", "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}"))
MAX_NM = 250
FRESH_S = 2.0          # an area's traffic is refetched at most this often
GAP_S = 0.4            # and upstream requests are spaced at least this far apart overall
EMERG = ("7500", "7600", "7700")


def credential(name):
    """A secret from a systemd credential file (LoadCredential=<name>, see deploy/), else the environment
    variable of the same name (opensky-client-id -> OPENSKY_CLIENT_ID)."""
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    if d:
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    return os.environ.get(name.upper().replace("-", "_"), "").strip()


def area_key(lat, lon, nm):
    nm = next(b for b in (15, 30, 60, 120, 180, MAX_NM) if nm <= b or b == MAX_NM)
    step = 0.1 if nm <= 30 else 0.25 if nm <= 60 else 0.5 if nm <= 120 else 1.0
    lat = max(-89.0, min(89.0, round(lat / step) * step))
    lon = (round(lon / step) * step + 540) % 360 - 180
    return round(lat, 3), round(lon, 3), nm


def convert(a, now, lat0, lon0):
    lat, lon = a.get("lat"), a.get("lon")
    if lat is None or lon is None or (a.get("seen_pos") or 0) > 60:
        return None
    ground = a.get("alt_baro") == "ground"
    alt = a.get("alt_geom", 0) if ground else a.get("alt_baro", a.get("alt_geom"))
    if not isinstance(alt, (int, float)):
        alt = 0 if ground else None
    if alt is None:
        return None
    sq = a.get("squawk") or ""
    em = a.get("emergency") or "none"
    emer = sq if sq in EMERG else (em.upper() if em != "none" else "")
    hexid = (a.get("hex") or "").lower()
    cs = (a.get("flight") or "").strip() or a.get("r") or hexid.upper()
    return {"id": hexid or cs, "cs": cs, "hex": hexid, "lat": round(lat, 5), "lon": round(lon, 5), "alt": int(alt),
            "gs": round(a.get("gs") or 0), "hdg": round(a.get("track", a.get("true_heading", 0)) or 0, 1),
            "vs": int(a.get("baro_rate", a.get("geom_rate", 0)) or 0), "type": a.get("t") or "", "reg": a.get("r") or "",
            "sq": sq, "air": not ground, "ground": ground, "emer": emer, "cat": a.get("category") or "",
            "ts": round(now - (a.get("seen_pos") or 0), 2), "dist": round(gc_nm(lat0, lon0, lat, lon), 1)}


class Traffic:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}            # key -> (monotonic time, state)
        self.last = -1e9

    def fetch(self, lat, lon, nm):
        for name, url in SOURCES:
            try:
                req = urllib.request.Request(url.format(lat=lat, lon=lon, nm=nm), headers={"User-Agent": "livescope/1"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    d = json.load(r)
                now = d.get("now") or time.time()
                return name, d.get("ac") or d.get("aircraft") or [], now / 1000 if now > 1e11 else now   # ms (adsb.lol) or s (adsb.fi)
            except Exception:  # noqa: BLE001 - try the next source
                continue
        return None, None, None

    def get(self, lat, lon, nm):
        key = area_key(lat, lon, nm)
        with self.lock:
            hit = self.cache.get(key)
            if hit and time.monotonic() - hit[0] < FRESH_S:
                return hit[1]
            wait = self.last + GAP_S - time.monotonic()
            if wait > 0:
                if hit:
                    return hit[1]
                time.sleep(wait)
            self.last = time.monotonic()
            src, acs, now = self.fetch(*key)
            if acs is None:
                st = {**(hit[1] if hit else {"aircraft": [], "t": time.time(), "src": None}), "ok": False}
            else:
                seen = {}
                for o in (convert(a, now, key[0], key[1]) for a in acs):
                    if o and (o["id"] not in seen or o["ts"] > seen[o["id"]]["ts"]):
                        seen[o["id"]] = o
                st = {"ok": True, "t": now, "src": src, "center": [key[0], key[1]], "nm": key[2], "aircraft": list(seen.values())}
            self.cache[key] = (time.monotonic(), st)
            if len(self.cache) > 64:
                for k in sorted(self.cache, key=lambda k: self.cache[k][0])[:16]:
                    del self.cache[k]
            return st



class World:
    """Every aircraft on Earth (airborne and on the ground) for the zoomed-out globe, from OpenSky's /states/all (the free ADS-B
    feeds above only answer for 250 nm around a point). Anonymous use allows ~100 snapshots a day, so one
    every 15 minutes; with a free OpenSky API client (opensky-client-id / opensky-client-secret from agent-secrets
    via deploy/install-service.sh, or OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET in the environment) one every 90 s. Fetched only while someone is looking, shared by everyone."""
    URL = "https://opensky-network.org/api/states/all"
    TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

    def __init__(self, routes):
        self.routes = routes
        self.nat = NatTracks()
        self.lock = threading.Lock()
        self.cid, self.secret = credential("opensky-client-id") or None, credential("opensky-client-secret")
        self.every = 90 if self.cid else 900
        self.body = json.dumps({"ok": False, "t": 0, "every": self.every, "ac": []}).encode()
        self.gz = gzip.compress(self.body)
        self.next_at = 0.0
        self.busy = False
        self.prev, self.lost = {}, {}
        self.token, self.token_exp = None, 0.0
        self.err, self.auth_retry = None, 0.0     # why the last fetch failed; when to try a rejected client again

    def auth(self):
        """The OpenSky client's bearer header. If OpenSky turns the client down (a wrong id or secret), fetch
        anonymously (every 15 min) and say so in the world body's err, trying the client again hourly."""
        if not self.cid or time.time() < self.auth_retry:
            return {}
        if time.time() > self.token_exp - 60:
            data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.cid, "client_secret": self.secret}).encode()
            try:
                with urllib.request.urlopen(urllib.request.Request(self.TOKEN_URL, data=data), timeout=10) as r:
                    j = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code not in (400, 401, 403):
                    raise
                self.auth_retry, self.every = time.time() + 3600, 900
                self.err = f"OpenSky turned down the API client (HTTP {e.code}): check opensky-client-id and -secret"
                print("livescope:", self.err, file=sys.stderr, flush=True)
                return {}
            self.token, self.token_exp = j["access_token"], time.time() + j.get("expires_in", 1800)
            self.every = 90
        return {"Authorization": "Bearer " + self.token}

    def fetch(self):
        wait = self.every
        try:
            req = urllib.request.Request(self.URL, headers={"User-Agent": "livescope/1", **self.auth()})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            t = d.get("time") or time.time()
            ac = []
            for st in d.get("states") or []:
                if st[5] is None or st[6] is None or not st[3] or t - st[3] > (600 if st[8] else 120):
                    continue
                alt = 0 if st[8] else st[7] if st[7] is not None else st[13]
                ac.append([st[0], round(st[6], 4), round(st[5], 4), round(st[10] or 0), round((st[9] or 0) * 1.94384),
                           round((alt or 0) * 3.28084 / 100), int(st[3]), (st[1] or "").strip(), 1 if st[8] else 0])
            est = self.estimate(t, ac)
            body = json.dumps({"ok": True, "t": t, "every": self.every, "src": "OpenSky", "ac": ac, "est": est,
                               **({"err": self.err} if self.err else {})}, separators=(",", ":")).encode()
            gz = gzip.compress(body, 6)
            with self.lock:
                self.body, self.gz = body, gz
            if not self.auth_retry or time.time() >= self.auth_retry:
                self.err = None
        except urllib.error.HTTPError as e:
            retry = e.headers.get("X-Rate-Limit-Retry-After-Seconds") if e.headers else None
            wait = max(self.every, int(retry)) if retry and retry.isdigit() else self.every
            self.fail(f"OpenSky answered HTTP {e.code}")
        except Exception as e:  # noqa: BLE001 - try again next time round
            wait = 120
            self.fail(f"OpenSky fetch failed: {type(e).__name__}: {e}")
        with self.lock:
            self.next_at = time.time() + wait
            self.busy = False

    def fail(self, msg):
        """Keep the last good snapshot, but tag the body with why the latest fetch failed (and log it)."""
        print("livescope:", msg, file=sys.stderr, flush=True)
        with self.lock:
            d = json.loads(self.body)
            d["err"] = msg
            self.body = json.dumps(d, separators=(",", ":")).encode()
            self.gz = gzip.compress(self.body, 6)

    def estimate(self, t, ac):
        """Aircraft that were cruising and then dropped out of every receiver's range (mostly over the oceans,
        where there's no free ADS-B), carried on at their last ground speed: along the North Atlantic Track
        they were lined up on (then to their scheduled destination, or straight on), else toward their
        scheduled destination (adsbdb route, when it fits their track), else straight on. A guess, drawn as
        such; dropped when the aircraft is seen again, reaches its destination, comes over an area with
        coverage, or after 4 hours without a route or track (an hour past a track's end; 13 with a route)."""
        seen = {r[0] for r in ac}
        cells = {}
        for r in ac:
            if not r[8]:
                k = (int(r[1] // 2), int(r[2] // 2))
                cells[k] = cells.get(k, 0) + 1
        for hx, r in self.prev.items():
            if hx not in seen and hx not in self.lost and not r[8] and r[5] >= 200 and r[4] >= 250:
                self.lost[hx] = {"cs": r[7], "lat": r[1], "lon": r[2], "trk": r[3], "gs": r[4], "alt": r[5], "ts": r[6]}
        self.prev = {r[0]: r for r in ac}
        routes = self.routes.get([v["cs"] for v in self.lost.values() if v["cs"]], background=True)
        tracks = self.nat.get()
        out = []
        for hx in list(self.lost):
            v = self.lost[hx]
            dt = t - v["ts"]
            if hx in seen or dt > 13 * 3600:
                del self.lost[hx]
                continue
            if "nat" not in v and tracks is not None:      # matched once, as it was when lost (the tracks change)
                v["nat"] = NatTracks.match(tracks, v["lat"], v["lon"], v["trk"], v["alt"], v["ts"])
            dist = v["gs"] * dt / 3600
            rt, to, nat = routes.get((v["cs"] or "").upper()), "", v.get("nat")
            dst = (rt[3], rt[4], rt[5]) if rt and rt[4] is not None and rt[5] is not None else None
            if nat:
                path = [(v["lat"], v["lon"])] + nat[1]
                end = path[-1]
                if dst and gc_nm(*end, dst[1], dst[2]) > 30 and \
                        abs((gc_brg(*end, dst[1], dst[2]) - gc_brg(*path[-2], *end) + 540) % 360 - 180) < 90:
                    path.append((dst[1], dst[2]))
                    to = f"{dst[0]} via NAT {nat[0]}"
                else:
                    to = f"NAT {nat[0]}"
                pos = along_path(path, dist)
                if pos is None:                            # past the end
                    total = sum(gc_nm(*a, *b) for a, b in zip(path, path[1:]))
                    if len(path) > len(nat[1]) + 1 or dist > total + v["gs"]:   # at its destination, or an hour on
                        del self.lost[hx]
                        continue
                    brg = gc_brg(*path[-2], *end)
                    la, lo = gc_dest(*end, brg, dist - total)
                    trk = gc_brg(la, lo, *gc_dest(*end, brg, dist - total + 10))
                elif len(path) > len(nat[1]) + 1 and pos[3] < 40:   # nearly there: it'll be seen on approach
                    del self.lost[hx]
                    continue
                else:
                    la, lo, trk = pos[:3]
            elif dst:
                left = gc_nm(v["lat"], v["lon"], dst[1], dst[2])
                if left > 150 and abs((gc_brg(v["lat"], v["lon"], dst[1], dst[2]) - v["trk"] + 540) % 360 - 180) < 50:
                    if dist >= left - 40:                # it has got there (or nearly: it'll be seen on approach)
                        del self.lost[hx]
                        continue
                    to = dst[0]
                    la, lo = gc_dest(v["lat"], v["lon"], gc_brg(v["lat"], v["lon"], dst[1], dst[2]), dist)
                    trk = gc_brg(la, lo, dst[1], dst[2])
            if not to:
                if dt > 4 * 3600:
                    del self.lost[hx]
                    continue
                la, lo = gc_dest(v["lat"], v["lon"], v["trk"], dist)
                la2, lo2 = gc_dest(v["lat"], v["lon"], v["trk"], dist + 10)
                trk = gc_brg(la, lo, la2, lo2)
            if dt > 1200 and cells.get((int(la // 2), int(lo // 2)), 0) >= 3:   # receivers there would see it
                del self.lost[hx]
                continue
            out.append([hx, round(la, 3), round(lo, 3), round(trk % 360), v["gs"], v["alt"], int(t), v["cs"], to, int(v["ts"])])
        return out

    def get(self):
        with self.lock:
            if not self.busy and time.time() >= self.next_at:
                self.busy = True
                threading.Thread(target=self.fetch, daemon=True).start()
            return self.body, self.gz


def along_path(path, nm):
    """(lat, lon, track, nm left) nm along a path of (lat, lon) points (great circles between them), or None past its end"""
    for i, (a, b) in enumerate(zip(path, path[1:])):
        seg = gc_nm(*a, *b)
        if nm <= seg:
            la, lo = gc_dest(*a, gc_brg(*a, *b), nm)
            return la, lo, gc_brg(la, lo, *b), seg - nm + sum(gc_nm(*p, *q) for p, q in zip(path[i + 1:], path[i + 2:]))
        nm -= seg
    return None


def xt_at(lat, lon, brg, plat, plon):
    """(cross-track, along-track) nm of a point from the great circle through (lat, lon) on bearing brg"""
    d = gc_nm(lat, lon, plat, plon) / R_NM
    a = math.radians(gc_brg(lat, lon, plat, plon) - brg)
    xt = math.asin(max(-1.0, min(1.0, math.sin(d) * math.sin(a))))
    at = math.acos(max(-1.0, min(1.0, math.cos(d) / max(1e-9, math.cos(xt)))))
    return xt * R_NM, (at if math.cos(a) >= 0 else -at) * R_NM


class NatTracks:
    """The North Atlantic Tracks (the day's organised routes across the North Atlantic, which most crossings fly:
    westbound in the day, eastbound at night), as published by NAV CANADA / NATS and relayed by VATSIM's natTrak.
    Only their lat/lon waypoints are kept (the oceanic part; the named fixes at the ends are on land's doorstep,
    where ADS-B sees the aircraft anyway). Fetched in the background every 30 minutes; a failed fetch keeps the
    last tracks."""
    URL = "https://nattrak.vatsim.net/api/tracks"
    EVERY, SLACK = 1800, 3600        # refetch; a track is still matched this long after it stops being valid

    def __init__(self):
        self.lock = threading.Lock()
        self.tracks, self.next_at, self.busy = None, 0.0, False

    @staticmethod
    def parse(d):
        out = []
        for tr in d if isinstance(d, list) else []:
            if not isinstance(tr, dict) or not tr.get("active"):
                continue
            pts = []
            for w in str(tr.get("last_routeing") or "").split():
                m = re.fullmatch(r"(\d{2})(\d{2})?/(\d{2,3})", w)    # 58/20 = 58N 20W, 5530/20 = 55°30'N 20W
                if m:
                    pts.append((int(m[1]) + int(m[2] or 0) / 60, -float(m[3])))
            if len(pts) < 2:
                continue
            fls = [f // 100 for f in tr.get("flight_levels") or [] if isinstance(f, int)]
            def ts(k):
                try:
                    return calendar.timegm(time.strptime(tr[k][:19], "%Y-%m-%dT%H:%M:%S"))
                except Exception:  # noqa: BLE001 - no usable time: don't limit by it
                    return None
            way = {"west": -1, "east": 1}.get(str(tr.get("direction") or "").lower())   # waypoints in flying order
            if way and (pts[-1][1] - pts[0][1]) * way < 0:
                pts.reverse()
            out.append({"id": str(tr.get("identifier") or "?"), "pts": pts, "way": way, "fl": (min(fls), max(fls)) if fls else (290, 410),
                        "from": ts("valid_from"), "to": ts("valid_to")})
        return out

    def fetch(self):
        try:
            req = urllib.request.Request(self.URL, headers={"User-Agent": "livescope/1"})
            with urllib.request.urlopen(req, timeout=15) as r:
                tracks = self.parse(json.load(r))
            with self.lock:
                self.tracks, self.next_at = tracks, time.time() + self.EVERY
        except Exception as e:  # noqa: BLE001 - keep the last tracks, try again in 5 minutes
            print(f"livescope: NAT tracks fetch failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            with self.lock:
                self.next_at = time.time() + 300
        with self.lock:
            self.busy = False

    def get(self):
        """the tracks (None until the first fetch); starts a refetch in the background when due"""
        with self.lock:
            if not self.busy and time.time() >= self.next_at:
                self.busy = True
                threading.Thread(target=self.fetch, daemon=True).start()
            return self.tracks

    @staticmethod
    def match(tracks, lat, lon, trk, alt, ts):
        """(track id, its waypoints still ahead) for an aircraft lined up on a track when it was last seen (on
        it, or heading for its first waypoint within 500 nm, the way the track is flown, at one of its levels
        give or take 2000 ft), the closest such; else None."""
        best = None
        for tr in tracks:
            if tr["from"] and ts < tr["from"] - 1800 or tr["to"] and ts > tr["to"] + NatTracks.SLACK:
                continue
            if not tr["fl"][0] - 20 <= alt <= tr["fl"][1] + 20:
                continue
            for pts in (tr["pts"],) if tr.get("way") else (tr["pts"], tr["pts"][::-1]):   # no direction given: the heading picks
                i = next((i for i, p in enumerate(pts) if xt_at(lat, lon, trk, *p)[1] > 30), None)
                if i is None:
                    continue
                a, b = (pts[i - 1], pts[i]) if i else (pts[0], pts[1])
                seg = gc_brg(*a, *b)
                if abs((seg - trk + 540) % 360 - 180) > 30:
                    continue
                if i:                                    # on the track: off it by
                    off = abs(xt_at(*a, seg, lat, lon)[0])
                else:                                    # short of its start: heading for it within
                    if gc_nm(lat, lon, *pts[0]) > 500:
                        continue
                    off = abs(xt_at(lat, lon, trk, *pts[0])[0])
                if off < 40 and (best is None or off < best[0]):
                    best = (off, tr["id"], list(pts[i:]))
        return best and (best[1], best[2])


class Routes:
    """Scheduled routes by callsign from adsbdb.com (crowd-sourced, so sometimes out of date: the page only
    believes one when the aircraft's position and climb/descent fit it). Looked up in the background,
    a few a second, and kept for 6 hours (unknown callsigns for 1)."""
    URL = "https://api.adsbdb.com/v0/callsign/{}"
    TTL, TTL_NONE, PER_S = 6 * 3600, 3600, 4

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}            # callsign -> (time, [orig, olat, olon, dest, dlat, dlon] or None)
        self.queue = []
        self.wake = threading.Event()
        threading.Thread(target=self.worker, daemon=True).start()

    def lookup(self, cs):
        req = urllib.request.Request(self.URL.format(cs), headers={"User-Agent": "livescope/1"})
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                fr = (json.load(r).get("response") or {}).get("flightroute")
        except urllib.error.HTTPError as e:
            return None if e.code == 404 else "retry"
        except Exception:  # noqa: BLE001 - network trouble: try again later
            return "retry"
        if not isinstance(fr, dict):
            return None
        o, d = fr.get("origin") or {}, fr.get("destination") or {}
        if not o.get("icao_code") or not d.get("icao_code"):
            return None
        return [o["icao_code"], o.get("latitude"), o.get("longitude"), d["icao_code"], d.get("latitude"), d.get("longitude")]

    def worker(self):
        while True:
            self.wake.wait()
            with self.lock:
                cs = self.queue.pop(0) if self.queue else None
                if not self.queue:
                    self.wake.clear()
            if cs is None:
                continue
            r = self.lookup(cs)
            with self.lock:
                if r == "retry":
                    self.cache.pop(cs, None)
                else:
                    self.cache[cs] = (time.time(), r)
            time.sleep(1 / self.PER_S)

    def get(self, callsigns, background=False):
        """known routes now; unknown ones are queued (a page's asks ahead of background ones) and show up on a later call"""
        now, out = time.time(), {}
        with self.lock:
            for cs in callsigns:
                cs = cs.strip().upper()
                if not cs.isalnum() or len(cs) > 8:
                    continue
                hit = self.cache.get(cs)
                if hit and hit[0] and now - hit[0] < (self.TTL if hit[1] else self.TTL_NONE):
                    out[cs] = hit[1]
                elif not (hit and hit[0] == 0):          # not already queued
                    self.cache[cs] = (0, None)
                    if background:
                        self.queue.append(cs)
                    else:
                        self.queue.insert(0, cs)
            if len(self.queue) > 3000:                    # too far behind: drop the background tail
                for cs in self.queue[3000:]:
                    self.cache.pop(cs, None)
                del self.queue[3000:]
            if self.queue:
                self.wake.set()
            if len(self.cache) > 50000:
                for k in [k for k, v in self.cache.items() if v[0] and now - v[0] > self.TTL_NONE]:
                    del self.cache[k]
        return out


