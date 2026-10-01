# ADR 0001: Use LocationIQ for road routes

Status: **accepted for routing provider; selective search proposed**  
Date: 2026-10-01  
Related documents: [API plan](../fuel-route-api-plan.md) · [Implementation plan](../fuel-route-implementation-plan.md)

## Decision

Use LocationIQ Directions for map routes and LocationIQ Matrix for selected road-distance checks. Do not build or operate a local road router. Python and PostGIS will do the station search, fuel calculation, and cost optimization. The user chose LocationIQ as the routing provider on 2026-10-01.

Straight-line distance from coordinates is useful for quickly removing impossible candidates, but it is not safe for a 500-mile driving-range check. The final Directions route and its leg distances must confirm every recommended stop.

## Three ways to use LocationIQ

| Choice | Accuracy | Provider calls | Use |
| --- | --- | --- | --- |
| **1. Directions only** | A simple station rule may miss cheaper plans. The final route can still be checked for range. | Lowest: direct route, then one or more final routes. | Baseline to compare against. |
| **2. Directions plus a small Matrix search** | Better stop and fuel choices. Matrix road distances guide a bounded search; final Directions legs verify the answer. | Moderate and capped. Matrix covers only promising station pairs. | **Recommended first release.** |
| **3. Several route alternatives plus a wider Matrix search** | May find cheaper roads and stops outside the first corridor. Still limited by returned alternatives and price data. | Highest latency and API use; may need a background job. | Upgrade if benchmarks show a useful cost gain. |

## Recommended flow: choice 2

1. Geocode the start and finish if they are addresses. Cache repeated lookups. Ask Directions once for the direct driving route and full GeoJSON geometry.
2. If that route is at most 500 miles, return it with no fuel stops. The assumed full tank covers it.
3. Use PostGIS to find US stations near the route. In Python, place them along the route and use straight-line lower bounds to discard only pairs that cannot be within 500 miles. Keep stations spread across the trip.
4. Ask Matrix for road distance and time **only for promising directed pairs**. Batch requests within the provider's coordinate limit. Never use `fallback_speed` to invent a drivable leg.
5. Optimize stop order and gallons purchased on the backend. Ask Directions for the chosen stops in travel order. Check its snapped waypoints, actual leg distances, detour, and fuel balance. Retry a different plan within a call/time budget if this one fails.
6. Return the final Directions geometry and costs. Say “optimized estimate within the searched corridor,” not “globally cheapest.”

The aim is one direct Directions call, a small number of Matrix batches for trips that need fuel, and one final Directions call. These are design targets, not guaranteed counts: long routes may need waypoint chunks, and failed plans may require another verification call. Measure real call counts and p95 time before setting the cap.

## Rules that protect accuracy

- A station near a drawn line may be far away by road. Use Matrix or Directions road distance before treating it as reachable.
- Matrix distances are estimates for choosing stops. The **final Directions legs** decide whether the plan is safe and what the trip cost is.
- Keep the same driving profile and coordinate order (`longitude,latitude`) across requests. Directions follows the order of supplied stops.
- A request that reaches a provider-call or time cap is `planning_unavailable`, not proof that no fuel stop exists.
- A full 50-gallon starting tank and fuel bought after departure are working product assumptions. Verify the CSV's fuel type, unit, timestamp, and repeated-price rows before release.

## Evidence from the first spike

On 2026-10-01, Directions returned a Chicago-to-Denver route of 1,617,529.2 meters with 9,584 GeoJSON points. A three-point Matrix request returned 1,617,529.1 meters for that pair. A second Directions route via Omaha returned two legs; its Chicago-to-Omaha leg was **251.8 meters longer** than the Matrix estimate. This confirms the configured key can use both services and shows why final legs need verification. It does **not** establish production latency, quota headroom, or route accuracy for all US trips. See [step 1 results](../fuel-route-implementation-plan.md#step-1-progress-summary--2026-10-01).

## References

- [LocationIQ Directions](https://docs.locationiq.com/reference/directions): ordered waypoints, full GeoJSON route, and leg distances.
- [LocationIQ Matrix](https://docs.locationiq.com/docs/matrix-api): directed road distances and unreachable cells.
- [LocationIQ routing options](https://docs.locationiq.com/docs/routing-api): coordinate order and request limits.
- [PostGIS `ST_DWithin`](https://postgis.net/docs/ST_DWithin.html): indexed station search near a route.
