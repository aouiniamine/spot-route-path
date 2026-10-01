# Spotter

The Django project uses a PostGIS database. `apps.fuel_stations` owns the station
records and CSV import; `apps.geo` contains the LocationIQ integration. No HTTP
endpoints are defined for fuel stations.

## Local setup

The database runs in Docker on host port 5444. Django runs locally in `.venv`.
GeoDjango needs GDAL, GEOS, and PROJ installed on the host for its PostGIS
point field.

```sh
brew install gdal
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
docker compose up -d db
python manage.py migrate
```

Put your LocationIQ key in `.env` as `LOCATIONIQ_API_KEY=your-key`, then run:

```sh
python manage.py import_fuel_stations --limit 10
python manage.py import_fuel_stations
```

The command reads `data/truck-stations-and-prices.csv` by default. It stages
every CSV row, then forward geocodes unresolved rows one request at a time. It
saves results in batches of 100 and waits at least one second between LocationIQ
requests. Change these with `--batch-size` and `--request-interval` to fit your
plan. `--limit` stages all rows but geocodes only the requested number for a
trial; `--retry-no-match` retries addresses that previously returned no result.
Rerunning the command keeps successful geocodes and updates changed source rows.

LocationIQ candidates must match the CSV city and state. If the address query
has no match, the command tries the truckstop name with the city and state.
Highway exit descriptions can remain unmatched rather than storing a point in
the wrong city.

The model stores LocationIQ's display name, latitude, longitude, and a PostGIS
`geography(Point, 4326)` column. The point uses **longitude, latitude** order.
Rows without a geocoding match remain in the table with `no_match` status and
empty coordinates.

## Retry unresolved stations with OpenStreetMap

Set `NOMINATIM_CONTACT_EMAIL=you@example.com` in `.env`, then run:

```sh
python manage.py retry_fuel_stations_osm --limit 10
```

The command selects rows with `no_match` or `error` status and no location,
checks Nominatim's city and state, then updates latitude, longitude, display
name, and the PostGIS point. It records attempted rows and caches successful
and unsuccessful searches across runs. Changed CSV addresses clear the attempt
marker. The management command sleeps between requests so each public API
request starts at least 16 seconds after the previous one. It accepts at most 100 rows
per command run. Run only one worker on one machine.
Public Nominatim is suitable for small, one-time retries; for a large backlog,
set `NOMINATIM_BASE_URL` to a private Nominatim-compatible HTTPS endpoint.
Follow the [public API usage policy](https://operations.osmfoundation.org/policies/nominatim/)
and provide [OpenStreetMap attribution](https://www.openstreetmap.org/copyright)
when displaying its data.
