"""livescope (https://livescope.kyleboas.com): real-world traffic from public ADS-B feeds."""
import acars
import adsb
import board
import boats
import hazards
import history
import swim
import timelapse
from scope.weather import Atis

SITE = {
    "name": "livescope",
    "title": "livescope",
    "url": "https://livescope.kyleboas.com",
    "old_hosts": ["liveatc."],            # the site's first name, 301-redirected here
    "port": 8799,
    "about": "real ADS-B traffic anywhere, on a STARS-style scope that zooms out to the globe",
    # links shown in the page: the feed by name, and an aircraft's page by its ICAO hex
    "src_links": {"adsb.lol": "https://adsb.lol", "adsb.fi": "https://adsb.fi"},
    "hex_link": ["https://adsb.lol/?icao={hex}", "{HEX} on adsb.lol"],
    "world_link": ["https://opensky-network.org", "OpenSky"],
    # live audio in the airport card: ICAO -> [[label, stream URL], ...], any MP3/AAC/Ogg stream the browser can
    # play (an Icecast mount from your own receiver, say). LiveATC.net's streams need their permission first:
    # their terms forbid third-party use. e.g. {"KJFK": [["TWR 119.1", "https://…/kjfk_twr"]]}
    "audio": {},
}


def flights():
    """FAA TFMS flight data (swim.Flights) when its SCDS subscription is set up on this server, else None"""
    cfg = swim.config("tfms")
    if not cfg:
        return None
    if not swim.available():
        print("livescope: SWIM tfms is configured but the Solace library isn't installed (deploy/install-service.sh)", flush=True)
        return None
    fl = swim.Flights()
    swim.Feed("tfms", cfg, fl.on_message)
    return fl


def providers(airports=None):
    """The site's data: traffic is required; world, routes and atis are optional (left out = feature off)."""
    fl = flights()
    routes, traffic = adsb.Routes(fl, airports), adsb.Traffic()
    world = adsb.World(routes, fl)
    tl = timelapse.Timelapse(traffic, adsb.MAX_NM, world)
    ships = boats.Boats()
    hist = history.History(tl, airports)
    return {"traffic": traffic, "world": world, "routes": routes, "atis": Atis(),
            "max_nm": adsb.MAX_NM, "features": ["timelapse", "aircraft", "acars", "boats", "wx", "history", "listen", "board"] + (["fplan"] if fl else []),
            "endpoints": {"timelapse": tl.endpoint, "aircraft": traffic.detail, "boats": ships.get, "wx": hazards.Hazards().get, "acars": acars.Acars().endpoint,
                          "history": hist.endpoint, "board": board.Board(hist, airports, fl, routes).endpoint,
                          **({"fplan": lambda s: fl.plan(s("cs", 8))} if fl else {})}}
