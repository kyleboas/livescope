"""An airport's departures and arrivals board, and its traffic by the hour (when it's busiest).

/api/live/board?a=list&ids=KJFK,KLGA  ->  each airport in short, for the list of boards (Board.summary)
/api/live/board?a=top  ->  the 50 busiest big airports over the next 12 h, the same way (Board.top)
/api/live/board?id=KATL  ->  {ok, id, t, now_h, hours: [{h, dep, arr, src}], typical: [{h, dep, arr}], dep: [...], arr: [...], ...}
    hours: 12 h back to 12 h ahead, the hour's start (h, unix seconds) and its count of departures and arrivals:
        src "seen", movements seen in the world recording (OpenSky): takeoffs and landings near the airport;
        src "filed" (US airports, FAA SWIM TFMS), flights filed to take off or land then (wheels off / on), for the hours ahead and
        the current one (seen + filed so far); without TFMS an hour ahead is null.
    typical: the same hours on the previous one or two days (seen), averaged: what the hours ahead usually look like.
    dep / arr: the board's rows, [{cs, apt (to / from), t (best time: actual, else estimated), sched (TFMS's planned time),
        st (scheduled, departed, en route, landed, cancelled), src (filed / seen), hex}], an hour back to 12 ahead.

Movements come from the world recording's hourly index (history.py): each aircraft's reports through an hour, with
the last 15 min of the hour before and the first 15 of the one after (from the recording itself), give
- a takeoff: on the ground, then airborne; or first seen low (under 3,000 ft above an airport) and climbing.
- a landing: airborne, then on the ground; or last seen low and descending.
OpenSky reports every 90 s (15 min anonymously) and sees most big airports but not all, so the counts are what it saw.
Each hour's movements are saved in <timelapse dir>/recent-moves/<hour>.json ([[t, airport, 0 takeoff / 1 landing,
hex, callsign], ...]) once, about 20 min after the hour, and kept as long as the recording (48 h).
"""
import json
import math
import os
import threading
import time
from collections import defaultdict

from history import members, row, thin
from scope.core import gc_nm
from timelapse import ROLL_H

EDGE_S = 900            # the neighbouring hours' reports looked at, and the longest gap between two reports of one move
LOW_FT = 3000           # first or last seen this low above an airport (and climbing / descending): took off or landed there
FRESH_S = 30
TOP_S = 300             # the busiest airports are ranked again this often
CLASS_BONUS = {"L": 1.5, "M": 0.7}


class Grid:
    """airports (not heliports or closed ones) in 1° cells, for the nearest one to a point"""

    def __init__(self, airports):
        self.cells = defaultdict(list)
        for a in (airports.all if airports else []):
            if a[8] in ("L", "M", "S"):
                self.cells[(math.floor(a[5]), math.floor(a[6]))].append((a[0], a[5], a[6], a[7] or 0, a[8]))

    def near(self, lat, lon, nm):
        cy, cx = math.floor(lat), math.floor(lon)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for a in self.cells.get((cy + dy, (cx + dx + 180) % 360 - 180), ()):
                    d = gc_nm(lat, lon, a[1], a[2]) - CLASS_BONUS.get(a[4], 0)
                    if d <= nm:
                        yield a, d

    def on(self, p):
        """the airport a point on the ground is at"""
        best = min(self.near(p[1], p[2], 4), key=lambda x: x[1], default=None)
        return best[0][0] if best else ""

    def low(self, p, ft=LOW_FT):
        """the airport an airborne point is low over (no more than ft above it), within a 3° slope's reach: about
        300 ft a mile; big airports first"""
        best = None
        for a, d in self.near(p[1], p[2], 14):
            agl = p[3] - a[3]
            if agl > ft or d > 4 + max(0, agl) / 300:
                continue
            rank = ("LMS".find(a[4]), d)
            if best is None or rank < best[0]:
                best = (rank, a, agl)
        return (best[1][0], max(0, best[2])) if best else ("", 0)


def moves(pts, grid, t_lo, t_hi, start_ok=True):
    """[(t, airport, 0 takeoff / 1 landing)] in one aircraft's thinned points [t, lat, lon, alt, gs, trk, k]
    (estimates left out), for moves whose time falls in t_lo..t_hi"""
    out = []
    pts = [p for p in pts if p[6] != 2]
    for i, p in enumerate(pts):
        if p[6] != 0 or p[4] < 50:
            continue
        prev = pts[i - 1] if i and p[0] - pts[i - 1][0] < EDGE_S else None
        nxt = pts[i + 1] if i + 1 < len(pts) and pts[i + 1][0] - p[0] < EDGE_S else None
        # takeoff
        if prev and prev[6] == 1:
            apt = grid.on(prev)
            agl = 0
            if not apt:
                apt, agl = grid.low(p, 5000)
            else:
                agl = max(0, p[3] - next((a[3] for a, _ in grid.near(prev[1], prev[2], 6) if a[0] == apt), 0))
            if apt:
                out.append((max(prev[0], p[0] - agl / 35), apt, 0))
        elif prev is None and nxt and nxt[6] == 0 and nxt[3] > p[3] and (start_ok or p[0] - t_lo > EDGE_S):
            apt, agl = grid.low(p)
            if apt:
                out.append((p[0] - agl / 35, apt, 0))
        # landing
        if nxt and nxt[6] == 1:
            apt = grid.on(nxt)
            agl = 0
            if not apt:
                apt, agl = grid.low(p, 5000)
            else:
                agl = max(0, p[3] - next((a[3] for a, _ in grid.near(nxt[1], nxt[2], 6) if a[0] == apt), 0))
            if apt:
                out.append((min(nxt[0], p[0] + agl / 13), apt, 1))
        elif nxt is None and prev and prev[6] == 0 and prev[3] > p[3]:
            apt, agl = grid.low(p)
            if apt:
                out.append((p[0] + agl / 13, apt, 1))
    keep, last = [], {}
    for t, apt, k in sorted(out):
        if t_lo <= t < t_hi and not (last.get((apt, k)) and t - last[(apt, k)] < 1200):
            keep.append((round(t), apt, k))
        last[(apt, k)] = t
    return keep


class Moves:
    """every airport's takeoffs and landings over the recording's 48 h, by hour"""

    def __init__(self, hist, airports):
        self.h, self.grid = hist, Grid(airports)
        self.dir = os.path.join(hist.tl.dir, "recent-moves")
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()
        self.by_apt = defaultdict(list)      # airport -> [(t, k, cs, hex)]
        self.done = set()                    # hours loaded
        for f in sorted(os.listdir(self.dir)):
            if f.endswith(".json"):
                self.load(int(f.split(".")[0]))
        threading.Thread(target=self.worker, daemon=True).start()

    def load(self, hr):
        try:
            evs = json.load(open(os.path.join(self.dir, f"{hr}.json"), encoding="utf-8"))
        except (OSError, ValueError):
            return
        with self.lock:
            for t, apt, k, hx, cs in evs:
                self.by_apt[apt].append((t, k, cs, hx))
            self.done.add(hr)

    def worker(self):
        while True:
            try:
                now = time.time()
                for hr in reversed(self.h.hours()):        # newest first: the usual for the hours ahead fills in soonest
                    if hr in self.done or (hr + 1) * 3600 + EDGE_S + 120 > now:
                        continue
                    if not os.path.exists(os.path.join(self.h.dir, f"{hr}.json")):
                        continue                   # not indexed yet
                    self.build(hr)
                    time.sleep(1)
                self.prune()
            except Exception as e:  # noqa: BLE001 - try again next round
                print("livescope: airport movements:", type(e).__name__, e, flush=True)
            time.sleep(120)

    def prune(self):
        cut = int((time.time() - ROLL_H * 3600) // 3600)
        for f in os.listdir(self.dir):
            if f.endswith(".json") and int(f.split(".")[0]) < cut:
                os.remove(os.path.join(self.dir, f))
        with self.lock:
            self.done = {h for h in self.done if h >= cut}
            for apt in list(self.by_apt):
                evs = [e for e in self.by_apt[apt] if e[0] >= cut * 3600]
                if evs:
                    self.by_apt[apt] = evs
                else:
                    del self.by_apt[apt]

    def index(self, hr, t_from=None, t_to=None):
        """{hex: (points, cs)} of the aircraft low enough to matter in an indexed hour"""
        out = {}
        path = os.path.join(self.h.dir, f"{hr}.gz")
        if not os.path.exists(path):
            return out
        for line in (x for data in members(path) for x in data.split(b"\n") if x):     # a gzip member per shard
            hx, pts, cs = json.loads(line)
            if t_from is not None:
                pts = [p for p in pts if t_from <= p[0] < t_to]
            if any(p[6] == 1 or (p[6] == 0 and p[3] < 15000) for p in pts):
                out[hx] = (pts, cs)
        return out

    def raw_head(self, hr):
        """{hex: (points, cs)} from the first EDGE_S of an hour still being recorded (or not indexed yet)"""
        acc = defaultdict(lambda: [[], ""])
        for data in members(os.path.join(self.h.raw, f"{hr}.ndjson.gz")):
            for line in data.split(b"\n"):
                if not line:
                    continue
                try:
                    f = json.loads(line)
                except ValueError:
                    continue
                if f[0] >= hr * 3600 + EDGE_S:
                    break
                for r in f[1] or []:
                    p, cs = row(r)
                    a = acc[r[0]]
                    a[0].append(p)
                    a[1] = cs or a[1]
        return {hx: (thin(p), cs) for hx, (p, cs) in acc.items()}

    def build(self, hr):
        t0, t1 = hr * 3600, (hr + 1) * 3600
        cur = self.index(hr)
        before = self.index(hr - 1, t0 - EDGE_S, t0) if os.path.exists(os.path.join(self.h.dir, f"{hr - 1}.gz")) else None
        after = self.index(hr + 1, t1, t1 + EDGE_S) if os.path.exists(os.path.join(self.h.dir, f"{hr + 1}.json")) else self.raw_head(hr + 1)
        evs = []
        for hx, (pts, cs) in cur.items():
            seq = (before.get(hx, ([], ""))[0] if before else []) + pts + after.get(hx, ([], ""))[0]
            for t, apt, k in moves(thin(seq), self.grid, t0, t1, before is not None):
                evs.append([t, apt, k, hx, cs])
        tmp = os.path.join(self.dir, f"{hr}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(evs, f, separators=(",", ":"))
        os.replace(tmp, os.path.join(self.dir, f"{hr}.json"))
        self.load(hr)

    def at(self, apt):
        with self.lock:
            return list(self.by_apt.get(apt, ())), set(self.done)


class Board:
    def __init__(self, hist, airports, flights=None, routes=None):
        self.airports, self.flights, self.routes = airports, flights, routes
        self.moves = Moves(hist, airports)
        self.cache = {}
        self.tfms, self.tfms_t, self.tfms_lock = {}, 0, threading.Lock()
        self.top_c = None

    def filed(self, ids, now):
        """TFMS's flights from and to the airport (any of its codes): [(kind 0 dep / 1 arr, flight)], from an index of
        every airport's, made again every FRESH_S"""
        if not self.flights:
            return []
        with self.tfms_lock:
            if now - self.tfms_t > FRESH_S:
                idx = defaultdict(list)
                with self.flights.lock:
                    for f in self.flights.refs.values():
                        if f.get("dep"):
                            idx[f["dep"]].append((0, f))
                        if f.get("arr"):
                            idx[f["arr"]].append((1, f))
                self.tfms, self.tfms_t = idx, now
            return [x for i in ids for x in self.tfms.get(i, ())]

    def endpoint(self, s):
        if s("a", 6) == "top":
            return self.top()
        if s("a", 6) == "list":
            return self.summary(s("ids", 400).upper().split(",")[:30])
        ident = s("id", 8).upper()
        a = self.airports and self.airports.one(ident)
        if not a:
            return {"ok": False, "error": "unknown airport"}
        ident = a[0]
        now = time.time()
        hit = self.cache.get(ident)
        if hit and now - hit[0] < FRESH_S:
            return hit[1]
        out = self.build(a, now)
        if len(self.cache) > 200:
            self.cache = {k: v for k, v in self.cache.items() if now - v[0] < FRESH_S}
        self.cache[ident] = (now, out)
        return out

    def summary(self, ids):
        """?a=list&ids=KJFK,KLGA: each airport's next 12 h in short, for the list of boards: {ok, now_h, apts: [{id, name, city,
        src filed / usual / none, next: [dep, arr] this hour, up: [this hour's and the next 12's dep + arr], peak: [h, n]}]}"""
        now = time.time()
        out = [self.brief(a, now) for a in (self.airports.one(i) for i in ids if i and self.airports) if a]
        if len(self.cache) > 2000:
            self.cache = {k: v for k, v in self.cache.items() if now - v[0] < FRESH_S}
        return {"ok": True, "t": round(now), "now_h": int(now // 3600) * 3600, "apts": out}

    def top(self, n=50):
        """?a=top: the n busiest big airports over the next 12 h (filed in the US, else the usual), busiest first, in short"""
        now = time.time()
        hit = self.top_c
        if not (hit and now - hit[0] < TOP_S):
            big = [a for a in (self.airports.all if self.airports else []) if a[8] == "L"]
            ranked = sorted((self.brief(a, now) for a in big), key=lambda r: -sum(v or 0 for v in r["up"]))
            hit = self.top_c = (now, [r for r in ranked if any(r["up"])][:n])
        return {"ok": True, "t": round(now), "now_h": int(now // 3600) * 3600, "apts": hit[1]}

    def brief(self, a, now):
        hit = self.cache.get(("s", a[0]))
        if hit and now - hit[0] < FRESH_S:
            return hit[1]
        b = self.build(a, now, rows=False)
        ahead = [(h, t) for h, t in zip(b["hours"], b["typical"]) if h["h"] >= b["now_h"]]
        src = "filed" if b["filed"] else "usual" if any(t["dep"] is not None for _, t in ahead) else "none"
        up = [(h["dep"] + h["arr"]) if src == "filed" else (round(t["dep"] + t["arr"]) if t["dep"] is not None else None) for h, t in ahead]
        nx = ([ahead[0][0]["dep"], ahead[0][0]["arr"]] if src == "filed" else
              [round(ahead[0][1]["dep"]), round(ahead[0][1]["arr"])] if src == "usual" and ahead[0][1]["dep"] is not None else None)
        pk = max(((h["h"], n) for (h, _), n in zip(ahead, up) if n), key=lambda x: x[1], default=None)
        r = {"id": a[0], "name": a[2], "city": a[3], "src": src, "next": nx, "up": up, "peak": list(pk) if pk else None}
        self.cache[("s", a[0])] = (now, r)
        return r

    def build(self, a, now, rows=True):
        ident, now_h = a[0], int(now // 3600)
        ids = {ident} | ({ident[1:]} if len(ident) == 4 and ident[0] == "K" else set()) | ({a[1]} if a[1] and a[4] == "US" else set())
        evs, done = self.moves.at(ident)
        seen = defaultdict(lambda: [0, 0])
        for t, k, cs, hx in evs:
            seen[int(t // 3600)][k] += 1
        fl = self.filed(ids, now)
        full = bool(fl) and a[4] == "US"                  # TFMS only has a foreign airport's flights to and from the US
        filed = defaultdict(lambda: [0, 0])
        want, rows = rows, [[], []]
        lo, hi = now - 3600, now + 12 * 3600
        for k, f in fl:
            t = f.get("on" if k else "off") or f.get("eta" if k else "etd")
            if not t:
                continue
            if f.get("status") != "CANCELLED":
                filed[int(t // 3600)][k] += 1
            if want and lo <= t <= hi:
                done_t = f.get("on" if k else "off")
                st = ("cancelled" if f.get("status") == "CANCELLED" else ("landed" if k else "departed") if done_t
                      else "en route" if k and (f.get("off") or f.get("status") == "ACTIVE") else "scheduled")
                rows[k].append({"cs": f["cs"], "apt": f.get("dep" if k else "arr") or "", "t": round(t), "st": st, "src": "filed",
                                **({"sched": round(f["etd" if not k else "eta"])} if f.get("etd" if not k else "eta") else {})})
        have = [{r["cs"] for r in rows[0]}, {r["cs"] for r in rows[1]}]
        for t, k, cs, hx in evs:
            if want and lo <= t <= now and (cs or hx) not in have[k]:
                rows[k].append({"cs": cs, "apt": "", "t": t, "st": "landed" if k else "departed", "src": "seen", "hex": hx})
        if want and self.routes:                          # where the seen ones went or came from (scheduled routes)
            got = self.routes.get([r["cs"] for k in (0, 1) for r in rows[k] if r["src"] == "seen" and r["cs"]], background=True)
            for k in (0, 1):
                for r in rows[k]:
                    rt = r["src"] == "seen" and got.get((r["cs"] or "").upper())
                    if rt and rt[3 if k == 0 else 0] != ident and rt[0 if k == 0 else 3] == ident:
                        r["apt"] = rt[3 if k == 0 else 0]
        for k in (0, 1):
            rows[k].sort(key=lambda r: r["t"])
        hours = []
        for h in range(now_h - 12, now_h + 13):
            if h < now_h and h in done:
                v, src = seen[h], "seen"
            elif h >= now_h and full:
                v, src = filed[h], "filed"
            else:
                v, src = None, "none"
            hours.append({"h": h * 3600, "dep": v[0] if v else None, "arr": v[1] if v else None, "src": src})
        typical = []
        for h in range(now_h - 12, now_h + 13):
            prev = [seen[h - d] for d in (24, 48) if h - d in done]
            typical.append({"h": h * 3600, "dep": round(sum(p[0] for p in prev) / len(prev), 1) if prev else None,
                            "arr": round(sum(p[1] for p in prev) / len(prev), 1) if prev else None})
        return {"ok": True, "id": ident, "t": round(now), "now_h": now_h * 3600, "hours": hours, "typical": typical,
                "dep": rows[0], "arr": rows[1], "filed": full, "seen_h": len(done), "src": "OpenSky" + (" + FAA SWIM" if fl else "")}
