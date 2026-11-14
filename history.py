"""Flight history: any aircraft's flights over the last two days, from the world recording livescope keeps anyway
(timelapse's "recent", OpenSky's worldwide snapshot every 90 s, or 15 min without an OpenSky API client), for the
aircraft card's list and the scope's replay.

/api/live/history?hex=  ->  {ok, hex, t, every_s, keep_h, flights:[{t0, t1, cs, from, to, top, gnd, live, pts}]}, oldest first
    pts: [[t, lat, lon, alt ft, gs kt, track °, k], ...], k 0 airborne, 1 on the ground, 2 estimated (over an ocean,
    out of receiver range: adsb.World's estimate); from/to: the airport it was on (or just above) at the start and end,
    '' when it wasn't near one; top: its highest altitude; gnd: it never left the ground; live: still flying.

Reading every snapshot for one aircraft would mean unpacking gigabytes, so each finished hour of the recording is
indexed once (in the background) into <timelapse dir>/recent-index/<hour>.gz: one gzip member per shard (the
address's last two hex digits), each line [hex, [point, ...]], and <hour>.json giving each shard's [offset, length].
A lookup reads one small member per hour, plus the hour still being recorded straight from its snapshots.
"""
import gzip
import json
import os
import struct
import threading
import time
import zlib
from collections import Counter, defaultdict

from adsb import HEX_RE
from scope.core import gc_nm
from timelapse import ROLL_H, ROLL_ID

FRESH_S = 60            # an aircraft's answer is reused for this long
TAXI_S = 25 * 60        # ground time kept before a takeoff and after a landing; a longer stay on the ground is parked
GAP_S = 45 * 60         # no reports this long, low down on both sides: it landed somewhere unseen
NEAR_NM = 4             # an airport this close to where a flight starts or ends is where it was
CLASS_BONUS = {"L": 1.5, "M": 0.7}     # nm: a big airport's reference point can be well off its runways
PACK = struct.Struct("<iiiihhb")      # a point while an hour is indexed: t, lat and lon x 1e4, alt, gs, track, k


def shard(hx):
    return hx[-2:]


def row(r, est=False):
    """an OpenSky compact row (adsb.World: [hex, lat, lon, trk, gs, alt/100, ts, cs, gnd]) or an estimate
    ([hex, lat, lon, trk, gs, alt/100, t, cs, to, last seen]) -> [t, lat, lon, alt ft, gs, trk, k], cs"""
    if est:
        return [int(r[6]), r[1], r[2], (r[5] or 0) * 100, r[4], r[3], 2], r[7]
    return [int(r[6]), r[1], r[2], 0 if r[8] else (r[5] or 0) * 100, r[4], r[3], 1 if r[8] else 0], r[7]


def thin(pts):
    """one point per report (OpenSky repeats a report until a newer one comes), and a parked aircraft's every 10 min"""
    out = []
    for p in sorted(pts):
        q = out[-1] if out else None
        if q and p[0] <= q[0]:
            if q[6] == 2 and p[6] != 2:
                out[-1] = p                    # a real report beats an estimate
            continue
        if q and p[6] == 1 and q[6] == 1 and p[0] - q[0] < 600 and abs(p[1] - q[1]) + abs(p[2] - q[2]) < 0.0003:
            continue
        out.append(p)
    return out


class History:
    def __init__(self, tl, airports=None):
        self.tl, self.airports = tl, airports
        self.raw = os.path.join(tl.dir, ROLL_ID)
        self.dir = os.path.join(tl.dir, ROLL_ID + "-index")
        os.makedirs(self.dir, exist_ok=True)
        self.cache, self.lock = {}, threading.Lock()
        self.cur = (None, 0, [])            # the hour being recorded: (hour, bytes read, its snapshot lines)
        threading.Thread(target=self.worker, daemon=True).start()

    # ------------------------------------------------------------------ the index
    def hours(self):
        try:
            return sorted(int(f.split(".")[0]) for f in os.listdir(self.raw) if f.endswith(".ndjson.gz"))
        except OSError:
            return []

    def worker(self):
        while True:
            try:
                now_h = int((time.time() - 120) // 3600)   # an hour's last snapshot can land just after it ends
                for hr in self.hours():
                    if hr < now_h and not os.path.exists(os.path.join(self.dir, f"{hr}.json")):
                        self.build(hr)
                        time.sleep(1)              # after a restart there can be two days to catch up on: leave the server room
                cut = int((time.time() - ROLL_H * 3600) // 3600)
                for f in os.listdir(self.dir):
                    if int(f.split(".")[0]) < cut:
                        os.remove(os.path.join(self.dir, f))
            except Exception as e:  # noqa: BLE001 - try again next round
                print("livescope: flight history index:", type(e).__name__, e, flush=True)
            time.sleep(120)

    def build(self, hr):
        """index a finished hour: its snapshots read one by one, each aircraft's points packed as they come"""
        acs = defaultdict(lambda: [bytearray(), Counter()])
        for data in members(os.path.join(self.raw, f"{hr}.ndjson.gz")):
            for line in data.split(b"\n"):
                try:
                    f = json.loads(line) if line else None
                except ValueError:
                    f = None
                if not f:
                    continue
                seen = set()
                for est, rows in ((False, f[1]), (True, f[2] if len(f) > 2 else None)):
                    for r in rows or []:
                        if est and r[0] in seen:
                            continue
                        seen.add(r[0])
                        (p, cs), a = row(r, est), acs[r[0]]
                        a[0] += PACK.pack(p[0], round(p[1] * 1e4), round(p[2] * 1e4), round(p[3]), max(-1, min(9999, round(p[4]))), round(p[5]) % 360, p[6])
                        if cs:
                            a[1][cs] += 1
        shards = defaultdict(list)
        for hx in list(acs):
            buf, names = acs.pop(hx)
            pts = thin([[t, la / 1e4, lo / 1e4, alt, gs, trk, k] for t, la, lo, alt, gs, trk, k in PACK.iter_unpack(buf)])
            shards[shard(hx)].append(json.dumps([hx, pts, names.most_common(1)[0][0] if names else ""], separators=(",", ":")))
        offs, pos = {}, 0
        with open(os.path.join(self.dir, f"{hr}.gz.tmp"), "wb") as out:
            for k, lines in shards.items():
                z = gzip.compress("\n".join(lines).encode(), 6)
                out.write(z)
                offs[k] = [pos, len(z)]
                pos += len(z)
        os.replace(os.path.join(self.dir, f"{hr}.gz.tmp"), os.path.join(self.dir, f"{hr}.gz"))
        with open(os.path.join(self.dir, f"{hr}.json.tmp"), "w", encoding="utf-8") as f:
            json.dump(offs, f)
        os.replace(os.path.join(self.dir, f"{hr}.json.tmp"), os.path.join(self.dir, f"{hr}.json"))

    def indexed(self, hr, hx):
        """[(point, cs)] for hx in an indexed hour, or None when the hour isn't indexed yet"""
        try:
            offs = json.load(open(os.path.join(self.dir, f"{hr}.json"), encoding="utf-8"))
        except (OSError, ValueError):
            return None
        o = offs.get(shard(hx))
        if not o:
            return []
        with open(os.path.join(self.dir, f"{hr}.gz"), "rb") as f:
            f.seek(o[0])
            data = gzip.decompress(f.read(o[1]))
        key = b'["' + hx.encode() + b'",'
        for line in data.split(b"\n"):
            if line.startswith(key):
                _, pts, cs = json.loads(line)
                return [(p, cs) for p in pts]
        return []

    def recording(self, hr, hx):
        """[(point, cs)] for hx in an hour not indexed yet (the one being recorded): its snapshots searched for the
        address (the current hour's are kept unpacked, read on from where they were last time)"""
        path = os.path.join(self.raw, f"{hr}.ndjson.gz")
        with self.lock:
            h, done, lines = self.cur if self.cur[0] == hr else (hr, 0, [])
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size > done:
                with open(path, "rb") as f:
                    f.seek(done)
                    data, used = read_members_bytes(f.read())
                lines = lines + [x for x in data.split(b"\n") if x]
                done += used
            if hr >= (self.cur[0] or 0):
                self.cur = (hr, done, lines)
        key, out = b'["' + hx.encode() + b'",', []
        for line in lines:
            i = line.find(key)
            while i >= 0:
                r = json.loads(line[i:line.index(b"]", i) + 1])
                out.append(row(r, len(r) > 9))
                i = line.find(key, i + 1)
        return out

    # ------------------------------------------------------------------ one aircraft's flights
    def near(self, p):
        """the airport a flight starts or ends at: the one it's on, or (caught low in the air) the biggest one near
        enough for its altitude, about 300 ft a mile, as on a 3° slope"""
        if not self.airports or p[6] == 2 or (p[6] == 0 and p[3] > 6000):
            return ""
        reach = NEAR_NM if p[6] == 1 else min(20, NEAR_NM + p[3] / 300)
        best = None
        for a in self.airports.area(p[1], p[2], reach + 2):
            d = gc_nm(p[1], p[2], a[5], a[6]) - CLASS_BONUS.get(a[8], 0)
            rank = (0 if p[6] == 1 else "LMSW".find(a[8]) % 4, d)        # in the air: big airports first
            if d <= reach and (best is None or rank < best[0]):
                best = (rank, a[0])
        return best[1] if best else ""

    def flights(self, got, now):
        cs_at = sorted((p[0], c) for p, c in got if c)
        pts = thin([p for p, _ in got])
        low = lambda p: p[6] == 1 or (p[6] == 0 and p[3] < 10000)  # noqa: E731
        # split: where it's on the ground between two airborne stretches (in the middle of that stay, or around a
        # long one), and at long gaps with it low down on both sides
        legs, cur, i = [], [], 0
        while i < len(pts):
            p = pts[i]
            if p[6] == 1:
                j = i
                while j < len(pts) and pts[j][6] == 1:
                    j += 1
                stay = pts[i:j]
                if not cur:                                   # before the first takeoff: its taxi out
                    cur = [q for q in stay if (pts[j][0] if j < len(pts) else stay[-1][0]) - q[0] < TAXI_S]
                elif j >= len(pts):                            # landed, still on the ground (or parked since)
                    cur += [q for q in stay if q[0] - stay[0][0] < TAXI_S]
                else:
                    if stay[-1][0] - stay[0][0] < 2 * TAXI_S:  # a turnaround: split in its middle
                        mid = (stay[0][0] + stay[-1][0]) / 2
                        cur += [q for q in stay if q[0] <= mid]
                        legs.append(cur)
                        cur = [q for q in stay if q[0] > mid]
                    else:                                      # in, parked, out again later
                        cur += [q for q in stay if q[0] - stay[0][0] < TAXI_S]
                        legs.append(cur)
                        cur = [q for q in stay if pts[j][0] - q[0] < TAXI_S]
                i = j
                continue
            if cur and p[0] - cur[-1][0] > GAP_S and low(cur[-1]) and low(p):
                legs.append(cur)
                cur = []
            cur.append(p)
            i += 1
        if cur:
            legs.append(cur)
        out = []
        for leg in legs:
            if len(leg) < 2:
                continue
            air = [p for p in leg if p[6] != 1]
            if not air and leg[-1][0] - leg[0][0] < 120:
                continue
            names = Counter(c for t, c in cs_at if leg[0][0] - 60 <= t <= leg[-1][0] + 60).most_common(1)
            live = now - leg[-1][0] < 1200 and leg[-1][6] != 1
            out.append({"t0": leg[0][0], "t1": leg[-1][0], "cs": names[0][0] if names else "", "from": self.near(leg[0]),
                        "to": "" if live else self.near(leg[-1]), "top": max((p[3] for p in air), default=0),
                        "gnd": not air, "live": live, "pts": leg})
        return out

    def endpoint(self, s):
        hx = s("hex", 7).lower()
        if not HEX_RE.fullmatch(hx) or hx.startswith("~"):
            return {"ok": False, "error": "bad hex"}
        now = time.time()
        hit = self.cache.get(hx)
        if hit and now - hit[0] < FRESH_S:
            return hit[1]
        got, waiting = [], 0
        for hr in self.hours():
            rows = self.indexed(hr, hx)
            if rows is None:
                if hr < int(now // 3600) - 1:
                    waiting += 1                   # an older hour still to be indexed (just after a restart)
                    continue
                rows = self.recording(hr, hx)
            got += rows
        rec = self.tl.recs.get(ROLL_ID) or {}
        out = {"ok": True, "hex": hx, "t": round(now, 1), "src": "OpenSky", "keep_h": ROLL_H, "first": rec.get("first"),
               "every_s": self.tl.world.every if self.tl.world else None, "flights": self.flights(got, now),
               **({"indexing_h": waiting} if waiting else {})}
        if len(self.cache) > 500:
            self.cache = {k: v for k, v in self.cache.items() if now - v[0] < FRESH_S}
        self.cache[hx] = (now, out)
        return out


def members(path):
    """each complete gzip member of a recording's hour file, unpacked one at a time"""
    try:
        raw = open(path, "rb").read()
    except OSError:
        return
    while raw:
        z = zlib.decompressobj(31)
        try:
            data = z.decompress(raw)
        except zlib.error:
            return
        if not z.eof:
            return
        yield data
        raw = z.unused_data


def read_members_bytes(raw):
    """(the data of the complete gzip members at the start of raw, how many bytes they took)"""
    out, used = [], 0
    while raw:
        z = zlib.decompressobj(31)
        try:
            data = z.decompress(raw)
        except zlib.error:
            break
        if not z.eof:
            break
        out.append(data)
        used += len(raw) - len(z.unused_data)
        raw = z.unused_data
    return b"".join(out), used
