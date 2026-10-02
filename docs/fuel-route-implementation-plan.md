# Fuel route API: implementation plan

Status: **implemented, verification in progress**  
Routing choice: [ADR 0001](adr/0001-fuel-route-planning-architecture.md)  
API contract: [Fuel route API plan](fuel-route-api-plan.md)

## What we will build

Add `POST /api/geo/route` to Django. It takes US start and finish coordinates (`lat`, `lng`), returns a GeoJSON driving route with fuel stops, and shows the amount paid for fuel bought on the trip.

**LocationIQ supplies the road route and selected road distances.** Python finds possible stations, chooses stops, calculates gallons, and totals the cost. PostGIS finds stations near a route. We will keep LocationIQ calls low by filtering stations on the backend before asking for road distances.

The working vehicle rules are a full 50-gallon tank at departure, 500 miles of range, and 10 miles per gallon. A trip within 500 miles needs no fuel purchase. The returned total excludes fuel already in the tank.

Treat `FuelStation.retail_price` as the price per gallon.

## Build order

### 1. Check the routing provider

- Use the configured LocationIQ key for a small, manual Directions and Matrix test. Check a long US route, full GeoJSON geometry, leg distances, and a small road-distance table. Keep the key out of logs and test output.
- Confirm the account's routing access, quota, coordinate limit, error responses, and real call time. Test a route through one or more stops before fixing the API call budget.
- Keep provider tests separate from automated tests. Saved responses will make the test suite fast and independent of the network.

**Done when:** Directions and Matrix work with this account and we have a measured budget for ordinary and long requests.

#### Step 1 progress summary — 2026-10-01

**Directions, Matrix, and a route through a stop passed; quota and error behavior still need checking.** The manual test in [`scripts/locationiq_routing_poc.py`](../scripts/locationiq_routing_poc.py) used the configured key without printing it. One run makes exactly **three external requests**:

| Check | Result |
| --- | --- |
| Directions: Chicago → Denver | 1,617,529.2 road meters, 64,644.8 seconds, 9,584 GeoJSON points, 615 ms |
| Matrix: Chicago, Omaha, Denver | Complete 3-by-3 distance/time table, 401 ms |
| Same Chicago → Denver pair in Matrix | 1,617,529.1 meters, within 0.1 meter of Directions |
| Directions: Chicago → Omaha → Denver | Two legs of 759,801.4 and 866,141.4 meters; total 1,625,942.8 meters; 9,836 GeoJSON points; 521 ms |

These measurements show that the key can use both routing APIs and that the returned data is usable. Matrix estimated Chicago → Omaha at 759,549.6 meters, **251.8 meters shorter** than the final Directions leg. This is why the final Directions legs must decide fuel range and cost. These are local measurements, not a production speed promise. Next in this step: check provider errors, rate limits, and the account's allowed request volume. The earlier local OSRM experiment is superseded by this provider choice.

### 2. Validate the two locations

- Accept only `start` and `finish`, each with numeric `lat` and `lng`. Reject addresses, extra fields, invalid numbers, and coordinates outside the supported radius.
- Use a local 1,700-mile radius around the approximate center of the contiguous US to reject distant coordinates before routing. This adds no provider calls. The radius is only an estimate and can accept Canada or Mexico while excluding Alaska and Hawaii.

**Done:** `FuelRouteRequest` accepts only numeric `lat`/`lng` for both endpoints. The view returns `400` for malformed input and `422` for coordinates outside the supported radius.

### 3. Find likely fuel stops

- Ask Directions once for the direct driving route. If it is at most 500 miles, return it with no stops; no Matrix call is needed.
- Search PostGIS for US stations near that route. Use its spatial index. In Python, place stations along the route and keep them spread across the trip so a low-price list cannot remove the only stop in a 500-mile stretch.
- Filter out stations without a point or a positive price. Run `deduplicate_fuel_stations` after import to retain only the lowest price row for each truckstop ID and address. The request reads the cleaned table directly.
- Use straight-line distance only as a **lower bound** to reject pairs that are certainly too far. Do not use it as driving distance or to prove that a station is reachable.
- Ask Matrix for road distance and time for the remaining promising directed pairs. Batch these calls within LocationIQ's coordinate limit. Do not use its straight-line `fallback_speed` option. Apply a measured cap on stations, pair checks, provider calls, and elapsed time.
- If the cap stops the search before coverage can be decided, return `planning_unavailable`, not “no stations exist.”

**Done:** PostGIS corridor search, US station filtering, coverage buckets, and one capped Matrix table are implemented. Bucket width grows on long routes so up to 23 stations cover the full trip in one Matrix request. Price row cleanup runs during import and is available as a separate management command.

### 4. Choose stops and gallons

- Optimize on the backend: choose which stations to visit and how much fuel to buy at each. A road leg uses its miles divided by 10 gallons. The tank must stay between zero and 50 gallons.
- Minimize money paid within the searched station set. Use less added driving, then fewer stops, to break equal-cost ties. Put a measured limit on detours so a distant cheap station does not cause an unreasonable drive.
- Use a bounded dynamic program with 0.1-gallon fuel states to choose stops. Recalculate exact purchase amounts using decimal math after the final Directions call. Return `planning_unavailable` when the search limit is reached.

**Done:** A bounded 0.1-gallon dynamic program selects stops. Final gallons use exact decimal arithmetic. Tests cover cheaper fuel earlier or later, partial fills, and an over-range leg.

### 5. Check the final route and return it

- Ask Directions for the route through chosen stops in travel order. Its **final leg distances** decide whether each leg is within fuel range. Check snapped station positions and the detour/border rules.
- Recalculate gallons and money from those final legs. Remove zero-purchase Matrix waypoints before the final Directions call when doing so does not increase estimated cost or driving distance. If final road distances still make a purchase zero, omit that pin from `fuel_stops` without another provider call.
- Return the final GeoJSON route, distance, duration, ordered stops, prices, gallons bought, total paid, chosen price rows, and search limits. Use decimal money math; the total must equal the sum of displayed stop costs.
- Label the answer an **optimized estimate within the searched corridor**. The first version does not prove the cheapest route across every US road.

**Done:** Final Directions legs drive range and purchase math. Zero-purchase Matrix waypoints are filtered before final routing when feasible; final zero purchases are omitted from the response without rerouting. A live Chicago to Denver check returned a 1,010.11-mile route before this optimization; a later live check hit LocationIQ HTTP 429.

### 6. Add the API and operations

- Add a thin Django view, URL, and typed request and response DTOs. Keep provider calls and fuel planning in services.
- Use per-call timeouts, a six-call provider budget, safe retries for provider 429/5xx responses, and a database cache for successful full responses. The cache expires after one day; station imports and cleanup invalidate it sooner when prices may change.
- Record candidate and provider-call counts in the response. Measure p95 latency and tune rate limiting against the account quota before production use.

**Done locally:** the endpoint and DTOs are documented, calls are bounded, and provider errors do not expose the key. The `routes_cache` table stores successful responses for one day. Endpoint rate limiting and p95 monitoring still need production infrastructure and quota measurements.

### Implementation summary — 2026-10-01

- Added `POST /api/geo/route` with strict coordinate request and typed response DTOs.
- Added the LocationIQ routing client, PostGIS corridor search, bounded fuel optimization, final Directions verification, and decimal price math.
- A short route normally uses one Directions call. A long route normally adds one Matrix and one final Directions call. No extra call is made for zero-purchase waypoints. The six-call budget allows limited retries.
- Automated endpoint, planner, provider, import, and cleanup tests pass. An additional [30-case suite](geo-route-test-cases.md) exercises request, radius, and fuel behavior without the CSV or live API. One live route succeeded before this optimization; a later live request received HTTP 429, so the updated route still needs a live check when quota permits.
- The station cleanup command removed 1,413 repeated rows from 678 physical truckstop groups in the local database. A second dry run found zero remaining groups. The import command now repeats cleanup after staging so those rows do not remain on a later import.
- A database cache returns repeated successful requests without rerunning the planner or provider calls. It expires after 24 hours and is cleared when station data is imported or cleaned. Cache hit, expiry, error, and request-key tests pass.

## Tests before release

| Test | What it must show |
| --- | --- |
| Fuel math | Trips under, at, and over 500 miles; several stops; cheaper early fuel; partial last purchase; no overfill. |
| Provider responses | Bad geometry, null Matrix cells, unreachable road pair, 429/5xx, timeout, waypoint order, and a final-route distance that differs from Matrix. |
| Data | US filtering, unlocated stations, conflicting price rows, and choosing one price per physical stop. |
| API | Bad inputs, no route, no station coverage, provider failure, call-budget limit, and safe errors. |
| Performance | Representative short, long, dense, and sparse trips meet agreed p95 time and LocationIQ-call budgets. |

Automated tests use saved LocationIQ responses and fixed station prices. Only a separate manual smoke test uses the live key.

## Files likely to change

| Area | Work |
| --- | --- |
| `apps/geo/clients/locationiq.py` or a new client beside it | Directions and Matrix methods with typed responses and safe errors. |
| `apps/fuel_stations/` | Import-time cleanup command for repeated physical stations. |
| `apps/geo/` | Request and response DTOs, routing client, candidate search, fuel optimizer, final checks, view, and tests. |
| `config/urls.py`, `config/settings.py`, `requirements.txt` | URL, provider budgets, caching, and tested solver dependency. |
| `docs/` | API examples, limits, price assumptions, and measured provider calls. |

## Decisions to close before release

1. Confirm that “total money spent” means purchases after starting with a full tank.
2. Confirm the Canada-border policy and how much extra driving is acceptable to save on fuel.
3. Approve the LocationIQ account quota and measured response-time/call budget.

## References

- [LocationIQ Directions](https://docs.locationiq.com/reference/directions): ordered road routes, legs, and GeoJSON geometry.
- [LocationIQ Matrix](https://docs.locationiq.com/docs/matrix-api): road distances between selected points.
- [PostGIS `ST_DWithin`](https://postgis.net/docs/ST_DWithin.html): indexed station search near a route.
