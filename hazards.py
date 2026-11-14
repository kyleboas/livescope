"""Airspace and weather hazards for livescope's map layers (feature "wx", /api/live/wx): TFRs, SIGMETs, AIRMETs, CWAs
and PIREPs, worldwide where the source covers it.

- TFRs: the FAA's tfr.faa.gov. The list of active ones, their shapes (its GeoServer) and each one's details
  (times, altitudes and NOTAM text, fetched once per TFR).
- SIGMETs (US, convective included, and international), G-AIRMETs (US, the current forecast), CWAs (US Center
  Weather Advisories) and PIREPs/AIREPs (the last 2 h, worldwide): aviationweather.gov's Data API.

Nothing needs a key. The whole world is fetched (well under 1 MB) when someone asks and the last copy is older than
FRESH_S, in the background; the request gets the last copy straight away.

/api/live/wx gives {ok, t, areas: [{k, id, name, hz, alt, from, to, text, poly: [[[lat, lon], ...], ...]}],
pireps: [[lat, lon, fl (hundreds of ft) or -1, kind (0 AIREP, 1 PIREP, 2 urgent), obs time, raw, turbulence, icing]]}.
k is tfr, sigmet, csigmet (convective), isigmet (international), airmet or cwa; from/to are unix seconds or null.
"""
import calendar
import json
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "livescope/1 (+https://github.com/kyleboas/livescope)"}
AWC = "https://aviationweather.gov/api/data/"
TFR_LIST = "https://tfr.faa.gov/tfrapi/exportTfrList"
TFR_SHAPES = ("https://tfr.faa.gov/geoserver/TFR/ows?service=WFS&version=1.1.0&request=GetFeature&typeName=TFR:V_TFR_LOC"
              "&maxFeatures=1000&outputFormat=application/json&srsname=EPSG:4326")
TFR_DETAIL = "https://tfr.faa.gov/download/detail_{}.xml"
FRESH_S = 300              # weather is refetched at most this often
TFR_S = 600                # and TFRs
TEXT_MAX = 3000
AIRMET_NAMES = {"IFR": "IFR", "MT_OBSC": "mountain obscuration", "TURB-HI": "turbulence (high)", "TURB-LO": "turbulence (low)",
                "ICE": "icing", "SFC_WND": "surface wind", "LLWS": "low-level wind shear", "M_FZLVL": "multiple freezing levels"}


def iso_t(s):
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def fetch(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def rings(geom):
    """GeoJSON (Multi)Polygon -> [[[lat, lon], ...], ...] outer rings, rounded"""
    if not geom:
        return []
    polys = [geom["coordinates"]] if geom.get("type") == "Polygon" else geom.get("coordinates") or [] if geom.get("type") == "MultiPolygon" else []
    return [[[round(p[1], 3), round(p[0], 3)] for p in poly[0]] for poly in polys if poly and len(poly[0]) >= 3]


def ft(v):
    """an altitude in feet -> 'SFC', 'FL180' or '3000'"""
    if v is None or v == "":
        return ""
    try:
        v = int(float(v))
    except ValueError:
        return str(v)
    return "SFC" if v <= 0 else f"FL{v // 100:03d}" if v >= 18000 else str(v)


def hundreds(v):
    """a G-AIRMET level: '130' (hundreds of feet), 'SFC' or 'FZL'"""
    v = (v or "").strip()
    return ft(int(v) * 100) if v.isdigit() else v


def band(lo, hi):
    lo, hi = ft(lo), ft(hi)
    return f"{lo or 'SFC'}–{hi}" if hi else f"above {lo}" if lo and lo != "SFC" else ""


class Hazards:
    def __init__(self):
        self.lock = threading.Lock()
        self.out = {"ok": False, "t": 0, "areas": [], "pireps": []}
        self.wx = {"sigmet": [], "isigmet": [], "airmet": [], "cwa": [], "pireps": []}
        self.tfrs, self.tfr_info = [], {}       # tfr areas; notam id -> details
        self.wxT = self.tfrT = 0.0
        self.busy = False

    def get(self, s=None):
        now = time.time()
        with self.lock:
            go = not self.busy and (now - self.wxT > FRESH_S or now - self.tfrT > TFR_S)
            if go:
                self.busy = True
            first = not self.out["ok"]
        if go:
            th = threading.Thread(target=self.refresh, daemon=True)
            th.start()
            if first:
                th.join(30)
        return self.out

    def refresh(self):
        try:
            now = time.time()
            if now - self.wxT > FRESH_S:
                if self.load_wx():
                    self.wxT = now
                else:
                    self.wxT = now - FRESH_S + 60
            if now - self.tfrT > TFR_S:
                if self.load_tfrs():
                    self.tfrT = now
                else:
                    self.tfrT = now - TFR_S + 60
            w = self.wx
            self.out = {"ok": bool(self.wxT or self.tfrT), "t": round(time.time()),
                        "areas": self.tfrs + w["sigmet"] + w["isigmet"] + w["airmet"] + w["cwa"], "pireps": w["pireps"]}
        finally:
            with self.lock:
                self.busy = False

    # ---------------------------------------------------------------- aviationweather.gov
    def awc(self, what):
        try:
            return json.loads(fetch(AWC + what, 45)).get("features") or []
        except Exception as e:  # noqa: BLE001 - one product being down leaves the last copy
            print(f"livescope: aviationweather.gov {what.split('?')[0]}: {e}", flush=True)
            return None

    def load_wx(self):
        got = 0
        f = self.awc("airsigmet?format=geojson")
        if f is not None:
            got += 1
            self.wx["sigmet"] = [{"k": "csigmet" if p.get("hazard") == "CONVECTIVE" else "sigmet",
                                  "id": f"{p.get('icaoId')} {p.get('seriesId')}", "name": f"SIGMET {p.get('seriesId') or ''}".strip(),
                                  "hz": p.get("hazard") or "", "alt": band(p.get("altitudeLow1") or p.get("altitudeLow2"), p.get("altitudeHi2") or p.get("altitudeHi1")),
                                  "from": iso_t(p.get("validTimeFrom")), "to": iso_t(p.get("validTimeTo")),
                                  "text": (p.get("rawAirSigmet") or "")[:TEXT_MAX], "poly": rings(x.get("geometry"))}
                                 for x in f for p in [x.get("properties") or {}] if p.get("airSigmetType") == "SIGMET"]
        f = self.awc("isigmet?format=geojson")
        if f is not None:
            got += 1
            self.wx["isigmet"] = [{"k": "isigmet", "id": f"{p.get('firId')} {p.get('seriesId')}",
                                   "name": f"SIGMET {p.get('seriesId') or ''} {(p.get('firName') or '').split(' ', 1)[-1].title()}".strip(),
                                   "hz": " ".join(x for x in (p.get("qualifier"), p.get("hazard")) if x),
                                   "alt": band(p.get("base"), p.get("top")), "from": iso_t(p.get("validTimeFrom")), "to": iso_t(p.get("validTimeTo")),
                                   "text": (p.get("rawSigmet") or "")[:TEXT_MAX], "poly": rings(x.get("geometry"))}
                                  for x in f for p in [x.get("properties") or {}]]
        f = self.awc("gairmet?format=geojson")
        if f is not None:
            got += 1
            out = []
            for x in f:
                p = x.get("properties") or {}
                hz = p.get("hazard") or ""
                if hz not in AIRMET_NAMES:          # FZLVL: freezing level contours, not areas
                    continue
                vt = iso_t(p.get("validTime"))
                out.append({"k": "airmet", "id": f"{p.get('product')} {p.get('tag')} {hz}", "name": f"G-AIRMET {(p.get('product') or '').title()} {p.get('tag') or ''}".strip(),
                            "hz": hz, "alt": f"{hundreds(p.get('base')) or 'SFC'}–{hundreds(p['top'])}" if p.get("top") else "",
                            "from": None, "to": vt + 5400 if vt else None,      # a snapshot for its time ±90 min
                            "text": "; ".join(x for x in (AIRMET_NAMES[hz] + (" " + p["severity"] if p.get("severity") else ""),
                                                          p.get("dueTo") and "due to " + p["dueTo"],
                                                          p.get("fzlbase") and f"freezing level {hundreds(p['fzlbase'])}–{hundreds(p.get('fzltop'))}",
                                                          vt and "forecast for " + time.strftime("%H%MZ", time.gmtime(vt))) if x),
                            "poly": rings(x.get("geometry"))})
            self.wx["airmet"] = out
        f = self.awc("cwa?format=geojson")
        if f is not None:
            got += 1
            self.wx["cwa"] = [{"k": "cwa", "id": f"{p.get('cwsu')} {p.get('seriesId')}", "name": f"CWA {p.get('cwsu') or ''} {p.get('seriesId') or ''}".strip(),
                               "hz": " ".join(x for x in (p.get("qualifier"), p.get("hazard")) if x), "alt": band(p.get("base"), p.get("top")),
                               "from": iso_t(p.get("validTimeFrom")), "to": iso_t(p.get("validTimeTo")),
                               "text": (p.get("cwaText") or "")[:TEXT_MAX], "poly": rings(x.get("geometry"))}
                              for x in f for p in [x.get("properties") or {}]]
        f = self.awc("pirep?format=geojson&age=2&bbox=-90,-180,90,180")
        if f is not None:
            got += 1
            out = []
            for x in f:
                p, g = x.get("properties") or {}, x.get("geometry") or {}
                c = g.get("coordinates") if g.get("type") == "Point" else None
                if not c:
                    continue
                kind = {"PIREP": 1, "Urgent PIREP": 2}.get(p.get("airepType"), 0)
                tb = " ".join(v for v in (p.get("tbFreq1"), p.get("tbInt1"), p.get("tbType1")) if v)
                ic = " ".join(v for v in (p.get("icgInt1"), p.get("icgType1")) if v)
                out.append([round(c[1], 3), round(c[0], 3), p["fltlvl"] if isinstance(p.get("fltlvl"), int) else -1, kind,
                            iso_t(p.get("obsTime")), (p.get("rawOb") or "")[:600], tb, ic])
            self.wx["pireps"] = out
        return got > 0

    # ---------------------------------------------------------------- tfr.faa.gov
    def load_tfrs(self):
        try:
            active = {x["notam_id"]: x for x in json.loads(fetch(TFR_LIST)) if x.get("notam_id")}
            shapes = json.loads(fetch(TFR_SHAPES, 40)).get("features") or []
        except Exception as e:  # noqa: BLE001
            print(f"livescope: tfr.faa.gov: {e}", flush=True)
            return False
        by_id = {}
        for f in shapes:
            nid = (f.get("id") or "").partition(".")[2]
            if nid in active:
                by_id.setdefault(nid, []).extend(rings(f.get("geometry")))
        new = [n for n in by_id if n not in self.tfr_info]
        with ThreadPoolExecutor(6) as ex:
            for nid, d in zip(new, ex.map(self.tfr_detail, new)):
                self.tfr_info[nid] = d
        for nid in [n for n in self.tfr_info if n not in active]:
            del self.tfr_info[nid]
        out = []
        for nid, polys in by_id.items():
            a, d = active[nid], self.tfr_info.get(nid) or {}
            out.append({"k": "tfr", "id": nid, "name": f"TFR {nid} {a.get('description') or ''}".strip(), "hz": (a.get("type") or "").title(),
                        "alt": d.get("alt", ""), "from": d.get("from"), "to": d.get("to"), "text": d.get("text", ""), "poly": polys})
        self.tfrs = out
        return True

    @staticmethod
    def tfr_detail(nid):
        """a TFR's times (UTC in the XML), altitudes and NOTAM text, from its XNOTAM file"""
        try:
            root = ET.fromstring(fetch(TFR_DETAIL.format(nid.replace("/", "_"))).decode("utf-8-sig"))
        except Exception:  # noqa: BLE001 - shown without them
            return {}
        g = lambda tag: (root.findtext(".//" + tag) or "").strip()      # noqa: E731

        def alt(which):
            v, uom, code = g("valDistVer" + which), g("uomDistVer" + which), g("codeDistVer" + which)
            if not v:
                return ""
            if uom == "FL":
                return "FL" + v.zfill(3)
            return "SFC" if v == "0" and code in ("HEI", "SFC") else v + (" AGL" if code == "HEI" else "")
        lo, hi = alt("Lower"), alt("Upper")
        text = re.sub(r"\s+", " ", g("txtDescrTraditional"))
        return {"from": iso_t(g("dateEffective")), "to": iso_t(g("dateExpire")), "alt": f"{lo or 'SFC'}–{hi}" if hi else "",
                "text": text[:TEXT_MAX]}
