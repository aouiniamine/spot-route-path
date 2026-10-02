# Fuel route API plan

Status: **implemented, pending live end-to-end check**. See the [implementation plan](fuel-route-implementation-plan.md) and [architecture decision](adr/0001-fuel-route-planning-architecture.md).

## Goal

Accept a start and finish in the US. Return a drivable map route, suggested fuel stops, and the estimated money paid for fuel on the trip. The vehicle has a 500-mile range and uses 10 miles per gallon.

Driving miles and route geometry come from **LocationIQ Directions**. Selected road-distance checks use **LocationIQ Matrix**. Python handles station selection, fuel use, and cost; PostGIS finds nearby stations. Filter candidates on the backend first to keep provider calls low.

## Endpoint

`POST /api/geo/route` with a JSON body:

```json
{
  "start": {"lat": 41.8781, "lng": -87.6298},
  "finish": {"lat": 39.7392, "lng": -104.9903}
}
```

The request DTO is `FuelRouteRequest(start: CoordinateRequest, finish: CoordinateRequest)`. `CoordinateRequest` has numeric `lat` and `lng` only. Addresses and additional fields are rejected. The backend estimates whether each point is near the contiguous US using a 1,700-mile radius from the approximate center near Lebanon, Kansas (39°50′ N, 98°35′ W). This check makes no LocationIQ request. A radius can accept nearby Canadian or Mexican points and excludes Alaska and Hawaii; it is not a country-boundary check. The center follows the [USGS geographic centers reference](https://www.usgs.gov/educational-resources/geographic-centers).

The response DTO is `FuelRouteResponse(route: RouteResponse, fuel_stops: list[FuelStopResponse], fuel: FuelSummaryResponse, planning: PlanningResponse, route_url: str | None)`. A stop's `location` is a `CoordinateResponse`. These typed Python DTOs are defined in [`apps/geo/dtos.py`](../apps/geo/dtos.py). Every successful HTTP response includes a `route_url` string. JSON keys follow the field names shown below.

Successful responses are cached in the `routes_cache` table for 24 hours. Each row has a UUID primary key; a separate unique hash uses the validated start and finish coordinates, so JSON whitespace and field order do not matter. A cache hit returns the saved JSON before the planner or LocationIQ client runs. Expired rows are ignored and removed when a new result is saved. Errors are never cached. Station import and cleanup clear the cache to avoid serving old prices after those operations.

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
    "location": {"lat": 41.0, "lng": -93.0},
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
    "total_fuel_cost_usd": "162.50"
  },
  "planning": {
    "status": "best_within_search",
    "search_scope": "stations near one driving corridor",
    "routing_provider": "LocationIQ",
    "candidate_count": 12,
    "provider_calls": 3
  },
  "route_url": "https://your-api.example/api/geo/route/550e8400-e29b-41d4-a716-446655440000"
}
```

GeoJSON coordinates are `[longitude, latitude]`. The route must pass through the displayed stops. Mile markers, total distance, and fuel use must come from the final routed legs.

`GET /api/geo/route/{uuid}` returns an HTML LocationIQ map preview that can be shared or embedded in an iframe. It draws the cached final route GeoJSON and fuel-stop markers, so opening it does not call Directions again. For example:

```html
<iframe src="https://your-api.example/api/geo/route/550e8400-e29b-41d4-a716-446655440000" title="Fuel route map" width="800" height="500"></iframe>
```

The preview returns `404` for unknown or expired UUIDs. Its URL stays valid for up to 24 hours and is removed sooner if station data is imported or cleaned. The browser loads LocationIQ map tiles using `LOCATIONIQ_MAPS_PUBLIC_KEY`. Configure a separate public token with HTTP referrer restrictions; if omitted, the server's `LOCATIONIQ_API_KEY` is used and exposed to the browser. The GET endpoint does not return a map image.

Use `400` for malformed input, `422` for coordinates outside the supported radius or a trip LocationIQ cannot drive, and `503` when the provider or planner is unavailable. An error has shape `{ "error": { "code": "planning_unavailable", "message": "..." } }`.

## Fuel and price rules

- A full tank at departure holds 50 usable gallons. A route of 500 miles or less needs no stop. `total_fuel_cost_usd` sums fuel bought **after departure**; it can be `$0.00` even though the trip used fuel.
- Each road leg uses `road_miles / 10` gallons. Check range using full-precision local road distances, not rounded displayed miles or straight-line miles. The tank cannot hold more than 50 gallons. Do not buy extra fuel merely to arrive with a full tank.
- Choose stops and purchase amounts to minimize the estimated amount paid, with a cap on extra driving. Use less driving and then fewer stops to break equal-cost ties. A single searched corridor cannot prove the cheapest possible route across every US road; report that limit.
- Treat `retail_price` as USD per gallon. Only use geocoded US stations with a positive price from the configured CSV source. The `deduplicate_fuel_stations` command keeps the lowest listed price row per truckstop ID and address before requests are served; import runs it after staging too. Return the retained source row. Prices are estimates from the imported file, not live quotes.
- Use decimal money math. Round each displayed stop cost to cents with `ROUND_HALF_UP`; make the total equal the sum of those displayed costs.

## Route search

1. Validate the two coordinates. Ask LocationIQ Directions for a direct driving route. If it is within 500 miles, return it without stops or Matrix calls.
2. Search PostGIS for stations near that route. Use Python to place candidates along the line and remove clearly impossible pairs. A point near the line is not proof of road access.
3. Ask LocationIQ Matrix for **selected** directed road distances, in small batches. Build a graph of legs no longer than 500 miles. Do not use straight-line fallback for an unreachable road pair.
4. In Python, choose a station path and purchase amounts. Ask Directions for the final route through those stations, then verify every leg and recalculate fuel and money from that route.
5. Return the map line, stops, costs, price source row/file, provider, and search limits. If the selected plan fails final checks, retry within a provider-call budget or return `503`.

The first release searches stations near one direct corridor. A wider multi-corridor search can be added later without changing the endpoint shape.

## Checks before release

- Test trips under, at, and over 500 miles; several stops; cheaper fuel earlier in the trip; partial final purchases; and no reachable station.
- Test station snapping, one-way roads, disconnected roads, and a route that is much longer than the straight line. Every returned Directions leg must fit the fuel available at its start.
- Test radius-limited inputs, US-only stations, duplicate price rows, provider failures, and timeouts.
- Benchmark LocationIQ calls, p95 API time, and cost quality on short and long US trips before setting final search limits.

## Decisions to confirm

1. Full starting tank and “money paid after departure” as the cost definition.
2. Border-crossing policy and maximum acceptable detour.

## References

- [LocationIQ Directions](https://docs.locationiq.com/reference/directions): road routes and GeoJSON geometry.
- [LocationIQ Matrix](https://docs.locationiq.com/docs/matrix-api): road distances between selected points.
- [PostGIS `ST_DWithin`](https://postgis.net/docs/ST_DWithin.html): indexed station search near a route.
