# livescope

A watch-only, STARS-style scope of real-world air traffic, live at https://livescope.kyleboas.com.

ADS-B positions come from adsb.lol, with adsb.fi as a fallback. The feed covers up to 250 nm around the
middle of the view, and positions are eased between reports. You can zoom out to an orthographic globe,
or search for any airport by ICAO, IATA or name. Close in, runways are drawn to scale.

### Features

- **Procedures:** US airports show their SIDs, STARs and approaches (FAA CIFP). Pick one in the airport
  card to highlight it with its fixes.
- **Airport card:** frequencies, the current ATIS (FAA D-ATIS through atis.info, US only) and the latest
  METAR (aviationweather.gov), plus a link to listen on LiveATC.net.
- **Ground maps:** zoomed in to an airport (GND / FLD), its taxiways, aprons, terminals and gates come
  from OpenStreetMap. The server fetches them once through Overpass, in the background, and caches them
  for 30 days in `livedata/ground/`. Taxiing aircraft leave history trails.
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
    carried on toward their scheduled destination as estimates (hollow rings).
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

Then open http://localhost:8799. Optional: `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` for the faster worldwide picture.

## Files

```
site.py   the site's settings and which providers it uses
adsb.py   ADS-B traffic (adsb.lol, adsb.fi), OpenSky world + ocean estimates, adsbdb routes
```

ATIS and METARs come from scope's `scope/weather.py`. Not for navigation. Data sources: [NOTICE.md](NOTICE.md). MIT ([LICENSE](LICENSE)).
