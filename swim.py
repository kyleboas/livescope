"""FAA SWIM flight data through SCDS (the SWIM Cloud Distribution Service): one queue per subscription,
read with Solace's Python API (solace-pubsubplus, the one outside library livescope uses; installed by
deploy/install-service.sh). SCDS allows that API without an exemption as long as compression is on.

A feed is configured by credentials (agent-secrets via deploy/install-service.sh, or the environment as
SWIM_TFMS_USERNAME etc.):
    swim-<feed>-username, swim-<feed>-password, swim-<feed>-queue    required
    swim-<feed>-url                                                  optional, default tcps://ems2.swim.faa.gov:55443
<feed> is tfms (VPN TFMS) or sfdps (VPN FDPS). A feed without them, or a server without the library, is off.
The username and queue name hold the account's e-mail address, so they're kept out of the repository too.
"""
import calendar
import os
import re
import sys
import threading
import time
from array import array

URL = "tcps://ems2.swim.faa.gov:55443"
VPNS = {"tfms": "TFMS", "sfdps": "FDPS"}
TRUST = ("/etc/ssl/certs", "/tmp/livescope-certs")     # a directory of CA certificates (Solace wants a directory)


def config(feed, secret=None):
    if secret is None:
        import adsb
        secret = adsb.credential
    c = {k: secret(f"swim-{feed}-{k}") for k in ("username", "password", "queue", "url")}
    if not (c["username"] and c["password"] and c["queue"]):
        return None
    return {**c, "url": c["url"] or URL, "vpn": VPNS[feed]}


def trust_dir():
    for d in TRUST:
        if os.path.isdir(d) and any(n.endswith((".pem", ".crt")) for n in os.listdir(d)):
            return d
    if os.path.isfile("/etc/ssl/cert.pem"):           # macOS: one bundle file, no directory
        os.makedirs(TRUST[1], exist_ok=True)
        with open("/etc/ssl/cert.pem", "rb") as f, open(os.path.join(TRUST[1], "cert.pem"), "wb") as g:
            g.write(f.read())
        return TRUST[1]
    return TRUST[0]


class Feed:
    """Reads one SCDS queue forever in a background thread, reconnecting on trouble, and hands each message's
    payload (bytes, usually FIXM XML) to on_message(feed, payload). Messages are acknowledged once handled,
    so the queue doesn't grow (and SCDS doesn't disable the subscription as idle)."""

    def __init__(self, feed, cfg, on_message):
        self.feed, self.cfg, self.on_message = feed, cfg, on_message
        self.state, self.count, self.err, self.last = "starting", 0, None, 0.0
        threading.Thread(target=self.run, daemon=True, name=f"swim-{feed}").start()

    def log(self, msg):
        print(f"livescope: SWIM {self.feed}: {msg}", file=sys.stderr, flush=True)

    def run(self):
        from solace.messaging.config.retry_strategy import RetryStrategy
        from solace.messaging.config.solace_properties import authentication_properties as A
        from solace.messaging.config.solace_properties import service_properties as S
        from solace.messaging.config.solace_properties import transport_layer_properties as T
        from solace.messaging.config.transport_security_strategy import TLS
        from solace.messaging.messaging_service import MessagingService
        from solace.messaging.receiver.message_receiver import MessageHandler
        from solace.messaging.resources.queue import Queue

        feed = self

        class Handler(MessageHandler):
            def on_message(self, m):
                try:
                    payload = m.get_payload_as_bytes()
                    if payload is None:
                        s = m.get_payload_as_string()
                        payload = s.encode() if s else b""
                    feed.count += 1
                    feed.last = time.time()
                    feed.on_message(feed.feed, payload)
                except Exception as e:  # noqa: BLE001 - Solace swallows callback errors: say what went wrong
                    feed.log(f"handling a message failed: {type(e).__name__}: {e}")
                finally:
                    try:
                        receiver.ack(m)
                    except Exception:  # noqa: BLE001
                        pass

        wait = 10
        while True:
            receiver = svc = None
            try:
                props = {T.HOST: self.cfg["url"], S.VPN_NAME: self.cfg["vpn"],
                         A.SCHEME_BASIC_USER_NAME: self.cfg["username"], A.SCHEME_BASIC_PASSWORD: self.cfg["password"],
                         T.COMPRESSION_LEVEL: 1}           # SCDS requires compression
                svc = (MessagingService.builder().from_properties(props)
                       .with_reconnection_retry_strategy(RetryStrategy.forever_retry(10000))
                       .with_transport_security_strategy(TLS.create().with_certificate_validation(
                           True, validate_server_name=True, trust_store_file_path=trust_dir()))
                       .build())
                self.state = "connecting"
                svc.connect()
                receiver = svc.create_persistent_message_receiver_builder().build(
                    Queue.durable_non_exclusive_queue(self.cfg["queue"]))
                receiver.start()
                receiver.receive_async(Handler())
                self.state, self.err, wait = "connected", None, 10
                self.log(f"connected to {self.cfg['url']} ({self.cfg['vpn']})")
                while svc.is_connected:
                    time.sleep(5)
                raise ConnectionError("disconnected")
            except Exception as e:  # noqa: BLE001 - try again, backing off to 10 minutes
                self.state, self.err = "down", f"{type(e).__name__}: {str(e)[:300]}"
                self.log(f"{self.err}; retrying in {wait} s")
                try:
                    if receiver:
                        receiver.terminate()
                    if svc:
                        svc.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(wait)
                wait = min(600, wait * 2)


def _ts(s):
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def _tag(name):
    return re.compile(r"<(?:[\w.-]+:)?%s\b([^>]*)>" % name)


MSG = re.compile(rb"<(?:[\w.-]+:)?fltdMessage\b([^>]*)>(.*?)</(?:[\w.-]+:)?fltdMessage>", re.S)
ATTR = re.compile(r'([\w:.-]+)="([^"]*)"')
ETD, ETA, WAYPOINT, DMS = _tag("etd"), _tag("eta"), _tag("waypoint"), _tag("(?:latitude|longitude)DMS")
STATUS = re.compile(r"<(?:[\w.-]+:)?flightStatus>\s*(\w+)")
REPORTED = re.compile(r"<(?:[\w.-]+:)?reportedPositionData\b.*?</(?:[\w.-]+:)?reportedPositionData>", re.S)
ALTITUDE = re.compile(r"<(?:[\w.-]+:)?altitude>\s*(\d+)\s*<")
ROUTE = re.compile(r"<(?:[\w.-]+:)?(?:routeOfFlight|newRouteOfFlight)\b[^>]*>([^<]+)<")
FIX = re.compile(r"<(?:[\w.-]+:)?fix\b([^>]*)>([^<]+)<")
TIME = re.compile(r"<(?:[\w.-]+:)?time>([^<]+)<")
WANTED = (b"waypoint", b"outeOfFlight", b'"ACTUAL"', b"reportedPositionData", b"flightStatus>", b"Cancellation")


def _deg(a):
    """a TFMS latitudeDMS / longitudeDMS element's attributes in degrees"""
    v = int(a.get("degrees") or 0) + int(a.get("minutes") or 0) / 60 + int(a.get("seconds") or 0) / 3600
    return -v if a.get("direction") in ("SOUTH", "WEST") else v


def parse(payload):
    """A TFMS flight-data message (tfmDataService XML, a batch of fltdMessages) as a list of dicts, one per
    flight message that says something livescope keeps, holding what it says (only the keys it has). Most
    are radar positions over the US, which ADS-B already has: of those only the time is kept (seen), as
    word that the flight is in the air.
      cs, ref (TFMS's flight id), kind (msgType), t (its time), dep, arr (airport codes, ICAO but for some
      3-letter US ones), status (PLANNED / ACTIVE / COMPLETED / CANCELLED), off (actual departure time),
      etd (planned), on (actual arrival), eta, path ([(lat, lon, seconds after departure)], TFMS's
      modelled trajectory along the filed route, gate to gate), ocean ((lat, lon, ft, time) of an oceanic
      position report: where it was seen out of radar coverage), seen (time of a radar position), route (the
      filed route as text, e.g. KORD..NOONY..OBH..CHESZ.Q88.HAKMN.ANJLL4.KLAX), fixes ((name, seconds after
      departure), the route's fixes in order: each is at the path point with the same time).
    Read with regular expressions, not an XML parser: TFMS sends ~40 of these a second, and this is ~10x faster."""
    out = []
    for m in MSG.finditer(payload):
        body = m[2]
        wanted = any(w in body for w in WANTED)
        if not wanted and b'"trackInformation"' not in m[1]:
            continue
        a = dict(ATTR.findall(m[1].decode("utf-8", "replace")))
        r = {"cs": (a.get("acid") or "").strip().upper(), "ref": a.get("flightRef"), "kind": a.get("msgType"),
             "t": _ts(a.get("sourceTimeStamp"))}
        if a.get("depArpt"):
            r["dep"] = a["depArpt"].upper()
        if a.get("arrArpt"):
            r["arr"] = a["arrArpt"].upper()
        if r["kind"] == "trackInformation":
            r["seen"] = r["t"]
        if not wanted:                                 # a radar position: just that it's flying
            out.append(r)
            continue
        body = body.decode("utf-8", "replace")
        for rx, typ, actual, planned in ((ETD, "etdType", "off", "etd"), (ETA, "etaType", "on", "eta")):
            for e in rx.findall(body):
                e = dict(ATTR.findall(e))
                t = _ts(e.get("timeValue"))
                if t:
                    r[actual if e.get(typ) == "ACTUAL" else planned] = t
        st = STATUS.search(body)
        if st:
            r["status"] = st[1]
        if r["kind"] == "flightPlanCancellation":
            r["status"] = "CANCELLED"
        wps = []
        for e in WAYPOINT.findall(body):
            e = dict(ATTR.findall(e))
            try:
                wps.append((int(e.get("sequenceNumber") or 0), float(e["latitudeDecimal"]), float(e["longitudeDecimal"]),
                            int(e.get("elapsedTime") or 0)))
            except (KeyError, ValueError):
                pass
        if len(wps) > 1:
            r["path"] = [w[1:] for w in sorted(wps)]
            fixes = []
            for e, name in FIX.findall(body):
                e = dict(ATTR.findall(e))
                try:
                    fixes.append((int(e.get("sequenceNumber") or 0), name.strip(), int(e.get("elapsedTime") or 0)))
                except ValueError:
                    pass
            if fixes:
                r["fixes"] = tuple(f[1:] for f in sorted(fixes))
        rt = ROUTE.search(body)
        if rt and rt[1].strip():
            r["route"] = re.sub(r"/\d{4}$", "", rt[1].strip())     # less the trailing /HHMM (time en route)
        rep = REPORTED.search(body)
        if rep:
            d = [_deg(dict(ATTR.findall(e))) for e in DMS.findall(rep[0])]
            alt, t = ALTITUDE.search(rep[0]), TIME.search(rep[0])
            t = t and _ts(t[1])
            if len(d) == 2 and t:
                r["ocean"] = (d[0], d[1], int(alt[1]) * 100 if alt else None, t)
        out.append(r)
    return out


class Flights:
    """What TFMS says about every flight to, from or over the US, by callsign: its airports, times, filed
    trajectory and latest oceanic position report. Fed by Feed("tfms", ...)'s messages; flights are dropped
    an hour after they land or are cancelled, or when nothing's been heard of them for 14 hours."""
    KEEP, QUIET = 3600, 14 * 3600

    def __init__(self):
        self.lock = threading.Lock()
        self.refs, self.by_cs = {}, {}      # TFMS flight id -> flight; callsign -> {ids}
        self.pruned = time.time()

    def on_message(self, feed, payload):
        if feed != "tfms":
            return
        for r in parse(payload):
            self.add(r)
        if time.time() - self.pruned > 600:
            self.prune()

    def add(self, r):
        ref, cs = r.get("ref"), r.get("cs")
        if not ref or not cs:
            return
        with self.lock:
            f = self.refs.get(ref)
            if f is None:
                f = self.refs[ref] = {"cs": cs}
            elif f["cs"] != cs:                          # a new callsign for the flight
                self.by_cs.get(f["cs"], set()).discard(ref)
                f["cs"] = cs
            self.by_cs.setdefault(cs, set()).add(ref)
            for k in ("dep", "arr", "status", "off", "etd", "on", "eta", "ocean", "seen", "route"):
                if r.get(k) is not None:
                    f[k] = r[k]
            if r.get("path"):                            # flat float32s: lat, lon, s, ... (a day's worth is ~25 MB)
                f["path"] = array("f", [x for p in r["path"] for x in p])
                f["fixes"] = r.get("fixes", ())
            f["t"] = time.time()

    def prune(self):
        now = time.time()
        with self.lock:
            self.pruned = now
            for ref, f in list(self.refs.items()):
                done = f.get("on") or f.get("status") in ("COMPLETED", "CANCELLED")
                if now - f["t"] > (self.KEEP if done else self.QUIET):
                    del self.refs[ref]
                    s = self.by_cs.get(f["cs"])
                    if s:
                        s.discard(ref)
                        if not s:
                            del self.by_cs[f["cs"]]

    def get(self, cs, now=None):
        """the flight flying under this callsign now (airborne: departed, or on radar or reported over the ocean
        lately, and not landed), else the one due
        off within 3 hours either side of now, else None. A copy, path as [(lat, lon, s)]."""
        now = now or time.time()
        with self.lock:
            best, key = None, None
            for ref in self.by_cs.get((cs or "").strip().upper(), ()):
                f = self.refs[ref]
                if f.get("status") == "CANCELLED":
                    continue
                up = f.get("off") or (f["ocean"][3] if f.get("ocean") and now - f["ocean"][3] < 6 * 3600 else None) \
                    or (f["seen"] if f.get("seen") and now - f["seen"] < 1800 else None)
                if up and not f.get("on") and f.get("status") != "COMPLETED" and now - up < 20 * 3600:
                    k = (0, -up)
                elif f.get("etd") and abs(f["etd"] - now) < 3 * 3600 and not f.get("off"):
                    k = (1, abs(f["etd"] - now))
                else:
                    continue
                if key is None or k < key:
                    best, key = f, k
            if best is None:
                return None
            f = dict(best)
        if "path" in f:
            p = f["path"]
            f["path"] = [(p[i], p[i + 1], p[i + 2]) for i in range(0, len(p), 3)]
        return f

    def plan(self, cs):
        """/api/live/fplan?cs=: a flight's filed plan for its card and the map: {dep, arr, route, status, off,
        eta, plan: {pts: [[fix, lat, lon]], line: [[lat, lon]] (the whole trajectory, gate to gate)}}, or {}
        when TFMS has none for it"""
        f = self.get(cs)
        if not f:
            return {}
        at = {round(p[2]): (round(p[0], 4), round(p[1], 4)) for p in f.get("path") or ()}
        first = f["path"][0] if f.get("path") else None
        pts = []
        for name, el in f.get("fixes") or ():
            p = at.get(el) or (first and el == 0 and (round(first[0], 4), round(first[1], 4)))
            if p:
                pts.append([name, *p])
        line = [[round(p[0], 4), round(p[1], 4)] for p in f.get("path") or ()]
        return {k: f[k] for k in ("dep", "arr", "route", "status", "off", "eta") if f.get(k)} | {"plan": {"pts": pts, "line": line}}

    def stats(self):
        with self.lock:
            return {"flights": len(self.refs), "with_path": sum(1 for f in self.refs.values() if "path" in f)}


def available():
    try:
        import solace.messaging  # noqa: F401
        return True
    except ImportError:
        return False
