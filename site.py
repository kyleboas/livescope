"""livescope (https://livescope.kyleboas.com): real-world traffic from public ADS-B feeds."""
import adsb
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


def providers():
    """The site's data: traffic is required; world, routes and atis are optional (left out = feature off)."""
    routes, traffic = adsb.Routes(), adsb.Traffic()
    world = adsb.World(routes)
    tl = timelapse.Timelapse(traffic, adsb.MAX_NM, world)
    return {"traffic": traffic, "world": world, "routes": routes, "atis": Atis(),
            "max_nm": adsb.MAX_NM, "features": ["timelapse"], "endpoints": {"timelapse": tl.endpoint}}
