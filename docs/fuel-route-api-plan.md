# Fuel route API plan

Status: **proposed for review**. See the [implementation plan](fuel-route-implementation-plan.md) and [architecture decision](adr/0001-fuel-route-planning-architecture.md).

## Goal

Accept a start and finish in the US. Return a drivable map route, suggested fuel stops, and the estimated money paid for fuel on the trip. The vehicle has a 500-mile range and uses 10 miles per gallon.

Driving miles and route geometry come from **LocationIQ Directions**. Selected road-distance checks use **LocationIQ Matrix**. Python handles station selection, fuel use, and cost; PostGIS finds nearby stations. Filter candidates on the backend first to keep provider calls low.

## Endpoint

`POST /api/routes/fuel-plan/` with a JSON body:

```json
{
  "start": {"address": "Chicago, IL"},
  "finish": {"address": "Denver, CO"}
}
```

Each place can instead be `{ "latitude": 41.88, "longitude": -87.63 }`. Use one form per place. Validate that both are in the 50 states or DC. A local US boundary check handles coordinate input without an extra provider request. Reject unclear or unresolved addresses.

Illustrative response shape, not a real quote:

```json
{
  "route": {
    "geometry": {"type": "LineString", "coordinates": [[-87.63, 41.88], [-93.0, 41.0], [-104.99, 39.74]]},
    "distance_miles": 1000.0,
    "duration_seconds": 60000
  },
  "fuel_stops": [{
    "station_id": 123,
    "source_row_number": 456,
    "name": "Example station",
    "address": "Example address",
    "location": {"latitude": 41.0, "longitude": -93.0},
    "mile_marker": 500.0,
    "price_per_gallon_usd": "3.25000000",
    "gallons_purchased": "50.000",
    "cost_usd": "162.50"
  }],
  "fuel": {
    "capacity_gallons": 50,
    "miles_per_gallon": 10,
    "initial_gallons": 50,
    "gallons_purchased": "50.000",
    "estimated_gallons_consumed": "100.000",
    "total_fuel_cost_usd": "162.50",
    "price_snapshot_id": "example-batch"
  },
  "planning": {
    "status": "best_within_search",
    "search_scope": "stations near one driving corridor",
    "routing_provider": "LocationIQ"
  }
}
```

GeoJSON coordinates are `[longitude, latitude]`. The route must pass through the displayed stops. Mile markers, total distance, and fuel use must come from the final routed legs. A map client can draw the geometry; the API does not return a map image.

Use `400` for malformed input, `422` for an unresolved place or a trip LocationIQ cannot drive, and `503` when the provider or planner is unavailable. Say “no station coverage” only when a complete search within the stated area proves it; a timeout or search cap is a `503`.

## Fuel and price rules

- A full tank at departure holds 50 usable gallons. A route of 500 miles or less needs no stop. `total_fuel_cost_usd` sums fuel bought **after departure**; it can be `$0.00` even though the trip used fuel.
- Each road leg uses `road_miles / 10` gallons. Check range using full-precision local road distances, not rounded displayed miles or straight-line miles. The tank cannot hold more than 50 gallons. Do not buy extra fuel merely to arrive with a full tank.
- Choose stops and purchase amounts to minimize the estimated amount paid, with a cap on extra driving. Use less driving and then fewer stops to break equal-cost ties. A single searched corridor cannot prove the cheapest possible route across every US road; report that limit.
- Only use geocoded US stations with valid, positive prices. The CSV has Canadian rows, repeated OPIS IDs, and no known fuel-product or price-date field. Confirm what the price means before release. Do not silently choose the cheapest of conflicting rows. Return the chosen price row and one complete price snapshot per plan.
- Use decimal money math. Round each displayed stop cost to cents with `ROUND_HALF_UP`; make the total equal the sum of those displayed costs.

## Route search

1. Resolve the two places. Ask LocationIQ Directions for a direct driving route. If it is within 500 miles, return it without stops or Matrix calls.
2. Search PostGIS for stations near that route. Use Python to place candidates along the line and remove clearly impossible pairs. A point near the line is not proof of road access.
3. Ask LocationIQ Matrix for **selected** directed road distances, in small batches. Build a graph of legs no longer than 500 miles. Do not use straight-line fallback for an unreachable road pair.
4. In Python, choose a station path and purchase amounts. Ask Directions for the final route through those stations, then verify every leg and recalculate fuel and money from that route.
5. Return the map line, stops, costs, price snapshot, provider, and search limits. If the selected plan fails final checks, retry within a provider-call budget or return `503`.

The first release searches stations near one direct corridor. A wider multi-corridor search can be added later without changing the endpoint shape.

## Checks before release

- Test trips under, at, and over 500 miles; several stops; cheaper fuel earlier in the trip; partial final purchases; and no reachable station.
- Test station snapping, one-way roads, disconnected roads, and a route that is much longer than the straight line. Every returned Directions leg must fit the fuel available at its start.
- Test US-only inputs/stations, the chosen Canada-border rule, duplicate price rows, price-snapshot changes, router failures, and timeouts.
- Benchmark LocationIQ calls, p95 API time, and cost quality on short and long US trips before setting final search limits.

## Decisions to confirm

1. Full starting tank and “money paid after departure” as the cost definition.
2. CSV fuel product, USD-per-gallon unit, timestamp, and duplicate-price rule.
3. Supported road coverage, border-crossing policy, and maximum acceptable detour.

## References

- [LocationIQ Directions](https://docs.locationiq.com/reference/directions): road routes and GeoJSON geometry.
- [LocationIQ Matrix](https://docs.locationiq.com/docs/matrix-api): road distances between selected points.
- [PostGIS `ST_DWithin`](https://postgis.net/docs/ST_DWithin.html): indexed station search near a route.
