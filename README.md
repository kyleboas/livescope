# livescope

A watch-only, STARS-style scope of real-world air traffic, live at https://livescope.kyleboas.com.

ADS-B positions come from adsb.lol, with adsb.fi as a fallback. The feed covers up to 250 nm around the
middle of the view, and positions are eased between reports. You can zoom out to an orthographic globe,
or search for any airport by ICAO, IATA or name. Close in, runways are drawn to scale.

### Features

- **Procedures:** US airports show their SIDs, STARs and approaches (FAA CIFP). Pick one in the airport
  card to highlight it with its fixes.
- **Airport card:** frequencies, the current ATIS (FAA D-ATIS through atis.info, US only) and the latest
  METAR (aviationweather.gov), plus LiveATC.net: its link opens LiveATC's page for the airport in a new tab,
  which follows the airport you pick and closes from the header (LiveATC ✕). You pick a feed
  there and it plays in LiveATC's own player; their terms rule out playing their streams on this page.
- **Live audio:** airports listed in `SITE["audio"]` (`site.py`) get ▶ buttons in their card that play the
  stream on the page; it keeps playing as you move around, and the header's ■ button stops it. Any stream
  the browser can play works, e.g. your own receiver through Icecast. None are listed yet: LiveATC.net's
  streams play fine but their terms forbid third-party use, so add theirs only with their permission.
- **Ground maps:** zoomed in to an airport (GND / FLD), its taxiways, aprons, terminals and gates come
  from OpenStreetMap. The server fetches them once through Overpass, in the background, and caches them
  for 30 days in `livedata/ground/`. Taxiing aircraft leave history trails.
- **Aircraft card:** click an aircraft for everything the feed has on it, as on ADS-B Exchange: type and
  its name, owner/operator, year built, country of registration, category, barometric and GPS altitude,
  ground/indicated/true speed and Mach, track and headings, roll, wind and air temperature, autopilot
  settings (selected altitude and heading, modes, QNH), signal (source, last seen, messages, RSSI) and
  accuracy (NACp, NIC, SIL ...), with a planespotters.net photo. IAS, Mach, wind and the like only come
  from aircraft that send them (mostly over Europe).
- **ALL / ARR / DEP / ARR+DEP** (bottom right) greys out and untags everything except the picked
  airport's arrivals and/or departures.
  - ADS-B has no flight plan, so these are guessed from track, altitude and climb/descent. Traffic goes
    to a nearby airport when it fits that one better.
  - Scheduled routes from adsbdb.com take precedence when the aircraft's position fits them.
- **ALT min–max** (header) greys out and untags traffic outside the band. It takes hundreds of feet,
  feet or FL350.
- **Worldwide traffic:** zoomed out past the 250 nm the live feeds cover, the globe shows every
  airborne aircraft as a dot, from OpenSky's `/states/all`.
  - Anonymously, that's one snapshot every 15 min. Set `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET`
    (a free OpenSky API client) to get one every 90 s.
  - No free feed covers the oceans, so airliners that leave every receiver's range at cruise are
    carried on as estimates (hollow rings). Over the North Atlantic, one lined up on one of the day's North
    Atlantic Tracks (from natTrak, refreshed every 30 min) follows that track's waypoints, then heads for its
    scheduled destination. Hovering it shows e.g. `→ KJFK via NAT C`. Anything else heads straight for its
    scheduled destination, or carries straight on.
- **Timelapses:** the clock button (top right) records the area in view on the server: one snapshot
  every 5 s, for 15 min to 24 h, carrying on with nobody watching. Any recording plays back in the scope at
  10× to 600×, eased between snapshots, with a scrubber; you can still pan, zoom, filter and click aircraft.
  - Zoomed out to the globe, it records the whole world instead: OpenSky's worldwide snapshot, one every
    90 s with an OpenSky API client (else every 15 min), played back at 60× to 3600×. About 100–400 KB
    a snapshot, so ~4–16 MB an hour with a client. One world recording at a time.
  - **The past:** the server also records the whole world all the time and keeps the last 48 h (about
    200–400 MB on disk with a client), so the top of the panel plays the last 1–48 h at any moment, of the
    globe or of the area in view (at OpenSky's detail, not the live feed's). `?tl=recent&h=6` links to it.
    This keeps OpenSky polled around the clock, which uses most of the daily allowance either way.
  - At most 4 record at once. Recordings are deleted 48 h after they finish (sooner if they pass 4 GB in total) in
    `livedata/timelapse/`, or `$LIVESCOPE_TIMELAPSE_DIR`. A busy airport at 60 nm takes about 2 MB an hour.
  - `?tl=<id>` in the URL opens a recording, so it can be shared.
  - Set `LIVESCOPE_TIMELAPSE_KEY` to make starting and stopping recordings need that key (typed once in the
    panel, then remembered by the browser). Without it, anyone can record. Watching is always open.
- **Split view:** the layout button (bottom left) cycles between one scope, big + small, and an even
  split. Each scope has its own view and layers; the second opens on the airport's field.
- **Measure:** right-drag or shift-drag (long-press then drag on touch) shows distance and bearing. If
  the line starts or ends on an aircraft, it also shows the time at that aircraft's ground speed.
- **Layers:** the stack button turns map layers on and off.
- **Views:** GND / FLD / APP / CTR / GLOBE set the zoom. The URL keeps the view (`#EGLL/40`,
  `#lat,lon,half-width-nm`).

## Run

It runs on [scope](https://github.com/kyleboas/scope), the shared scope server and client. Check both out side by side:

```
git clone https://github.com/kyleboas/scope
git clone https://github.com/kyleboas/livescope
python3 scope/server.py livescope --port 8799     # Python 3.10+, standard library only
```

Then open http://localhost:8799. Optional: `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` for the faster worldwide picture, and
`LIVESCOPE_TIMELAPSE_KEY` so only you can record timelapses.

On a server, `deploy/install-service.sh` installs it as a locked-down system service that takes the OpenSky
client from agent-secrets (`sudo secret livescope opensky-client-id`, `sudo secret livescope
opensky-client-secret`, then `sudo bash deploy/install-service.sh`) and pulls both repos every minute, so a
push to GitHub deploys.

## Files

```
deploy/        install-service.sh: the system service, its secrets and auto-deploy
site.py        the site's settings and which providers it uses
adsb.py        ADS-B traffic (adsb.lol, adsb.fi), OpenSky world + ocean estimates (North Atlantic Tracks), adsbdb routes
timelapse.py   records areas' traffic on the server and serves it back for playback
```

ATIS and METARs come from scope's `scope/weather.py`. Not for navigation. Data sources: [NOTICE.md](NOTICE.md). MIT ([LICENSE](LICENSE)).
