# Fuel route API: implementation plan

Status: **proposed for review**  
Routing choice: [ADR 0001](adr/0001-fuel-route-planning-architecture.md)  
API contract: [Fuel route API plan](fuel-route-api-plan.md)

## What we will build

Add `POST /api/routes/fuel-plan/` to Django. It will take a US start and finish, return a GeoJSON driving route with fuel stops, and show the amount paid for fuel bought on the trip.

**LocationIQ supplies the road route and selected road distances.** Python finds possible stations, chooses stops, calculates gallons, and totals the cost. PostGIS finds stations near a route. We will keep LocationIQ calls low by filtering stations on the backend before asking for road distances.

The working vehicle rules are a full 50-gallon tank at departure, 500 miles of range, and 10 miles per gallon. A trip within 500 miles needs no fuel purchase. The returned total excludes fuel already in the tank.

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

### 2. Make fuel prices safe to read

- Confirm what CSV `Retail Price` means: fuel type, USD-per-gallon unit, date, and why one OPIS ID can have several prices. Choose an offer only after these facts are known.
- Import prices into a complete new snapshot, then switch the active snapshot in one database transaction. The current importer updates rows in batches and could otherwise mix old and new prices in one plan.
- Keep the chosen source row and snapshot ID in the result. Use only US stations with a valid positive price and a geocoded point.

**Done when:** one request sees one complete price snapshot and each returned price can be traced to its row.

### 3. Resolve the two locations

- Accept either an address or latitude/longitude for each place. Cache address lookups. Use the existing LocationIQ geocoder for addresses; check coordinates against a local US boundary dataset so they need no extra provider request.
- Reject unclear addresses, invalid coordinates, and locations outside the US. Decide whether a US-to-US route may cross Canada; the working rule is to reject one until tested.

**Done when:** valid inputs become coordinates and invalid inputs get clear `400` or `422` errors.

### 4. Find likely fuel stops

- Ask Directions once for the direct driving route. If it is at most 500 miles, return it with no stops; no Matrix call is needed.
- Search PostGIS for US stations near that route. Use its spatial index. In Python, place stations along the route and keep them spread across the trip so a low-price list cannot remove the only stop in a 500-mile stretch.
- Use straight-line distance only as a **lower bound** to reject pairs that are certainly too far. Do not use it as driving distance or to prove that a station is reachable.
- Ask Matrix for road distance and time for the remaining promising directed pairs. Batch these calls within LocationIQ's coordinate limit. Do not use its straight-line `fallback_speed` option. Apply a measured cap on stations, pair checks, provider calls, and elapsed time.
- If the cap stops the search before coverage can be decided, return `planning_unavailable`, not “no stations exist.”

**Done when:** there is a small, ordered set of candidate stops with real road distances between the needed pairs.

### 5. Choose stops and gallons

- Optimize on the backend: choose which stations to visit and how much fuel to buy at each. A road leg uses its miles divided by 10 gallons. The tank must stay between zero and 50 gallons.
- Minimize money paid within the searched station set. Use less added driving, then fewer stops, to break equal-cost ties. Put a measured limit on detours so a distant cheap station does not cause an unreasonable drive.
- A continuous-fuel mixed-integer solver is the working approach. Compare its answers with hand-calculated small examples before pinning a package version. Return `planning_unavailable` if the solve cannot finish within the request budget.

**Done when:** tests cover several stops, cheaper fuel earlier, partial final fills, and a trip with no possible stop chain.

### 6. Check the final route and return it

- Ask Directions for the route through chosen stops in travel order. Its **final leg distances** decide whether each leg is within fuel range. Check snapped station positions and the detour/border rules.
- Recalculate gallons and money from those final legs. If the chosen plan fails, exclude it and try another within the provider-call budget. Never return fuel-stop pins on the original route when the car must drive a different route.
- Return the final GeoJSON route, distance, duration, ordered stops, prices, gallons bought, total paid, price snapshot, and search limits. Use decimal money math; the total must equal the sum of displayed stop costs.
- Label the answer an **optimized estimate within the searched corridor**. The first version does not prove the cheapest route across every US road.

**Done when:** a separate check can replay the returned legs and confirm the range and dollar totals.

### 7. Add the API and operations

- Add a thin Django view, URL, input validation, and response schema. Keep provider calls and fuel planning in services.
- Add request deadlines, safe retries for provider 429/5xx responses, caching, and an endpoint rate limit. Cache keys must include route direction and provider options; a full-plan cache must include the price snapshot.
- Measure p95 request time, Directions/Matrix calls, candidate count, solver time, verification retries, and error codes. Set provider-call and time caps from step 1 results and representative trips.

**Done when:** the endpoint is documented, measurable, bounded, and does not expose the LocationIQ key.

## Tests before release

| Test | What it must show |
| --- | --- |
| Fuel math | Trips under, at, and over 500 miles; several stops; cheaper early fuel; partial last purchase; no overfill. |
| Provider responses | Bad geometry, null Matrix cells, unreachable road pair, 429/5xx, timeout, waypoint order, and a final-route distance that differs from Matrix. |
| Data | US filtering, unlocated stations, conflicting price rows, and an import snapshot switch during a request. |
| API | Bad inputs, no route, no station coverage, provider failure, call-budget limit, and safe errors. |
| Performance | Representative short, long, dense, and sparse trips meet agreed p95 time and LocationIQ-call budgets. |

Automated tests use saved LocationIQ responses and fixed station prices. Only a separate manual smoke test uses the live key.

## Files likely to change

| Area | Work |
| --- | --- |
| `apps/geo/clients/locationiq.py` or a new client beside it | Directions and Matrix methods with typed responses and safe errors. |
| `apps/fuel_stations/` | Complete price snapshots and eligible-station search. |
| New `apps/routes/` | Candidate search, fuel optimizer, final checks, view, and tests. |
| `config/urls.py`, `config/settings.py`, `requirements.txt` | URL, provider budgets, caching, and tested solver dependency. |
| `docs/` | API examples, limits, price assumptions, and measured provider calls. |

## Decisions to close before release

1. Confirm the fuel product, price unit/date, and duplicate-price rule in the CSV.
2. Confirm that “total money spent” means purchases after starting with a full tank.
3. Confirm the Canada-border policy and how much extra driving is acceptable to save on fuel.
4. Approve the LocationIQ account quota and measured response-time/call budget.

## References

- [LocationIQ Directions](https://docs.locationiq.com/reference/directions): ordered road routes, legs, and GeoJSON geometry.
- [LocationIQ Matrix](https://docs.locationiq.com/docs/matrix-api): road distances between selected points.
- [PostGIS `ST_DWithin`](https://postgis.net/docs/ST_DWithin.html): indexed station search near a route.
