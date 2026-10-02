# Spotter

Spotter is a Django API for driving trips across the contiguous United States. Given start and finish coordinates, it returns a road route, suggested fuel stops, and estimated fuel spending. Route planning assumes a vehicle range of 500 miles and fuel economy of 10 miles per gallon.

## Architecture

- **Fuel stations (`apps/fuel_stations`)** — imports station prices and stores station locations in PostgreSQL with PostGIS.
- **Route planning (`apps/geo`)** — gets road routes and selected driving distances from LocationIQ, finds nearby stations with PostGIS, and uses Python to choose fuel stops and purchase amounts.
- **Route responses (`apps/geo`)** — verifies the final route, caches successful responses in PostgreSQL for one day, and serves a map preview of the saved path.
- **Project configuration (`config`)** — holds Django settings and URL routing.

The [architecture decision record](docs/adr/0001-fuel-route-planning-architecture.md) explains the routing and optimization choices.
