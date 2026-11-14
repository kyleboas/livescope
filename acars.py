"""ACARS for livescope: the datalink messages an aircraft has sent and received lately (VHF ACARS, VDL2, HFDL and
SATCOM), as heard by airframes.io's volunteer feeders. Anonymous use of their API is allowed at 60 requests a
minute per IP, so each aircraft's messages are cached for a minute and the server keeps well under that overall.
Attribution, as airframes.io asks: "Data provided by Airframes.io and its community of feeders".
"""
import calendar
import json
import threading
import time
import urllib.parse
import urllib.request

from adsb import HEX_RE

URL = "https://api.airframes.io/v1/messages?"
FRESH_S = 60           # an aircraft's messages are refetched at most this often
BUDGET = 30            # upstream requests a minute, all aircraft together (airframes.io allows 60)
SINCE_H = 12           # how far back to look
KEEP = 60              # messages per aircraft
TEXT_MAX = 2000

LINKS = {"acars": "VHF ACARS", "vdl": "VDL2", "hfdl": "HFDL", "aero-acars": "SATCOM", "satcom": "SATCOM", "iridium": "Iridium"}
# what the common labels carry (ARINC 618/620); messages with other labels just show their label
LABELS = {
    "H1": "FMS / airline", "AA": "CPDLC (uplink)", "BA": "CPDLC (downlink)", "A6": "ADS-C request", "B6": "ADS-C report",
    "A9": "ATIS", "B9": "ATIS request", "A0": "FANS application", "B0": "ATC logon", "AB": "ATC", "BB": "ATC", "A7": "clearance",
    "B3": "departure clearance request", "A3": "departure clearance", "A1": "oceanic clearance", "B2": "oceanic clearance request",
    "A4": "PDC acknowledgement", "C1": "free text to crew", "SA": "media advisory", "Q0": "link test", "_d": "general response",
    "5Z": "airline (out of range)", "80": "airline report", "10": "airline", "15": "airline", "16": "airline", "20": "airline",
    "21": "airline", "22": "airline", "2Z": "airline", "30": "airline", "37": "airline", "39": "airline", "44": "airline", "4A": "airline",
    "QA": "OUT report", "QB": "OFF report", "QC": "ON report", "QD": "IN report", "QE": "OUT / fuel", "QF": "OFF / destination",
    "QG": "OUT / return in", "QH": "OUT report", "QK": "landing", "QL": "arrival", "QM": "arrival", "QN": "diversion",
    "QP": "OUT", "QQ": "OFF", "QR": "ON", "QS": "IN", "QT": "OUT / return in", "54": "voice contact request", "57": "position report",
    "58": "position report", "RA": "printer (to crew)", "SQ": "ground station squitter",
}


def iso_t(s):
    """'2026-11-14T17:26:31.558Z' -> unix seconds"""
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) + float("0" + s[19:].rstrip("Z"))
    except (TypeError, ValueError):
        return None


def tidy(m):
    """One airframes.io message -> what the card shows, without the feeder's own details"""
    t = iso_t(m.get("timestamp") or m.get("createdAt"))
    text = (m.get("text") or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if t is None or not text:
        return None
    label, kind = m.get("label") or "", m.get("sourceType") or ""
    d = m.get("linkDirection")
    up = d == "uplink" if d in ("uplink", "downlink") else label in ("AA", "A6", "A9", "A7", "A3", "A1", "A4", "AB", "C1", "RA")
    st = m.get("station") or {}
    what = LABELS.get(label, "")
    for tag, w in ((".AT1.", "CPDLC (encoded)"), (".CR1.", "CPDLC connect request"), (".CC1.", "CPDLC connect confirm"),
                   (".DR1.", "CPDLC disconnect"), (".ADS.", "ADS-C (encoded)"), (".TI2/", "ATIS request")):
        if tag in text[:40]:
            what = w
            break
    return {"t": round(t, 1), "label": label, "what": what, "up": up,
            "link": LINKS.get(kind, kind), "freq": m.get("frequency"), "flight": m.get("flightNumber") or "",
            "near": st.get("nearestAirportIcao") or "", "text": text[:TEXT_MAX]}


def dedupe(msgs):
    """Several feeders often hear the same message: keep its first copy. Newest first."""
    out, seen = [], {}
    for m in sorted(msgs, key=lambda m: m["t"]):
        k = (m["label"], m["text"])
        if k in seen and m["t"] - seen[k] < 300:
            continue
        seen[k] = m["t"]
        out.append(m)
    return out[::-1][:KEEP]


class Acars:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}            # hex -> (fetched at, body)
        self.busy = {}             # hex -> Event, while it's being fetched
        self.calls = []            # times of recent upstream requests

    def fetch(self, hx):
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - SINCE_H * 3600))
        q = urllib.parse.urlencode({"icao": hx.upper(), "limit": 100, "since": since})
        req = urllib.request.Request(URL + q, headers={"User-Agent": "livescope/1", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = json.load(r)
        msgs = dedupe([m for m in map(tidy, raw if isinstance(raw, list) else []) if m])
        return {"ok": True, "hex": hx, "t": round(time.time(), 1), "since_h": SINCE_H, "msgs": msgs}

    def endpoint(self, s):
        """/api/live/acars?hex=: the aircraft's datalink messages over the last 12 h, newest first"""
        hx = s("hex", 7).lower()
        if not HEX_RE.fullmatch(hx) or hx.startswith("~"):
            return {"ok": False, "error": "bad hex"}
        now = time.time()
        with self.lock:
            hit = self.cache.get(hx)
            if hit and now - hit[0] < FRESH_S:
                return hit[1]
            ev = self.busy.get(hx)
            if not ev:
                self.calls = [c for c in self.calls if now - c < 60]
                if len(self.calls) >= BUDGET:
                    return hit[1] if hit else {"ok": False, "hex": hx, "error": "busy, try again in a minute"}
                self.calls.append(now)
                ev = self.busy[hx] = threading.Event()
                mine = True
            else:
                mine = False
        if not mine:
            ev.wait(10)
            with self.lock:
                hit = self.cache.get(hx)
            return hit[1] if hit else {"ok": False, "hex": hx, "error": "airframes.io didn't answer"}
        try:
            body = self.fetch(hx)
        except Exception as e:  # noqa: BLE001 (any failure upstream: say so, keep the last answer if there is one)
            body = dict(hit[1], stale=True) if hit else {"ok": False, "hex": hx, "error": f"airframes.io: {type(e).__name__}"}
        with self.lock:
            self.cache[hx] = (time.time(), body)
            if len(self.cache) > 2000:
                for k in sorted(self.cache, key=lambda k: self.cache[k][0])[:500]:
                    del self.cache[k]
            self.busy.pop(hx, None)
        ev.set()
        return body
