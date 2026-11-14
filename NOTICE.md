# Notices

livescope's code is MIT-licensed (see LICENSE). It runs on [scope](https://github.com/kyleboas/scope), whose NOTICE covers the
map, airport, procedure, ground-map, ATIS and METAR data. livescope's own data sources, fetched at run time and not stored:

- **Live positions** from adsb.lol (ODbL) or adsb.fi.
- **The worldwide picture** from The OpenSky Network (<https://opensky-network.org>, non-commercial use).
- **Scheduled routes** from adsbdb.com.
- **Owner, operator and year built** from adsb.fi's aircraft database; **aircraft photos** from planespotters.net (each
  credited to its photographer and linked to its page there).
- **Ships (AIS)** from aisstream.io (<https://aisstream.io>) and Fintraffic's Digitraffic (<https://www.digitraffic.fi>, CC BY 4.0).
- **North Atlantic Tracks** (the daily NAV CANADA / NATS track message) through VATSIM's natTrak (<https://nattrak.vatsim.net>).
- **FAA SWIM flight data** (TFMS, SFDPS) through the FAA's SWIM Cloud Distribution Service, for non-operational use only. Read with Solace's
  `solace-pubsubplus` Python API (Apache 2.0), which `deploy/install-service.sh` installs on the server; it isn't part of this repository.
