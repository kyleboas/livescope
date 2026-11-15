"""LiveATC.net feed for an airport frequency, so a frequency clicked in the card opens LiveATC's own player for it.
LiveATC's search pages sit behind a Cloudflare check the server can't pass, but its playlist files
(/play/<mount>.pls) answer plainly and give the feed's title. So the usual mount names are tried for the
frequency's position: kjfk_twr_1191 (the frequency in the name), kjfk_twr, kjfk_twr2. Feeds named otherwise
(kord1n2_gnd_meter) aren't found, and the page shows LiveATC's frequency search instead. Each mount is checked
at most once a day.
"""
import re
import threading
import time
import urllib.request

PLS = "https://www.liveatc.net/play/{}.pls"
FRESH_S = 86400
BUDGET = 60            # playlist requests a minute, all lookups together
# OurAirports' frequency types -> LiveATC's mount suffix for that position
SUFFIX = {"TWR": "twr", "GND": "gnd", "CLD": "del", "DEL": "del", "CLNC": "del", "ATIS": "atis", "D-ATIS": "atis",
          "APP": "app", "DEP": "dep", "CTAF": "ctaf", "UNIC": "ctaf"}
ID_RE = re.compile(r"[A-Z0-9]{3,4}")


class LiveAtc:
    def __init__(self):
        self.lock = threading.Lock()
        self.seen = {}          # mount -> (checked at, title or None)
        self.calls = []

    def title(self, mount):
        """The feed's title, or None if LiveATC has no such mount (or the budget is used up)"""
        now = time.time()
        with self.lock:
            hit = self.seen.get(mount)
            if hit and now - hit[0] < FRESH_S:
                return hit[1]
            self.calls = [c for c in self.calls if now - c < 60]
            if len(self.calls) >= BUDGET:
                return None
            self.calls.append(now)
        t = None
        try:
            req = urllib.request.Request(PLS.format(mount), headers={"User-Agent": "livescope/1"})
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read(2000).decode("latin-1")
            if body.startswith("[playlist]"):
                m = re.search(r"^Title1=(.*)$", body, re.M)
                t = m.group(1).strip() if m else mount
        except OSError:
            return None             # not cached: a hiccup isn't "no feed"
        with self.lock:
            self.seen[mount] = (now, t)
        return t

    def endpoint(self, s):
        """/api/live/liveatc?id=KJFK&t=TWR&f=119.100: {mount, title} of the feed, or {} when none is found"""
        icao, kind = s("id", 4).upper(), s("t", 8).upper()
        try:
            mhz = float(s("f", 8))
        except ValueError:
            return {}
        if not ID_RE.fullmatch(icao) or kind not in SUFFIX or not 108 <= mhz < 137:
            return {}
        i, suf, d = icao.lower(), SUFFIX[kind], f"{mhz:.3f}".replace(".", "")
        fd = d[:max(4, len(d.rstrip("0")))]          # 119.100 -> 1191, 121.875 -> 121875, 120.000 -> 1200
        for mount in (f"{i}_{suf}_{fd}", f"{i}_{suf}", f"{i}_{suf}2"):
            t = self.title(mount)
            if not t:
                continue
            other = [x for x in re.findall(r"\b1[1-3]\d\.\d{1,3}\b", t) if abs(float(x) - mhz) > 0.003]
            if other:               # its title names another frequency
                continue
            return {"mount": mount, "title": t}
        return {}
