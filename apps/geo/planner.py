"""Corridor search and bounded fuel-price optimization."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import math

from django.contrib.gis.geos import LineString
from django.contrib.gis.measure import D

from apps.fuel_stations.models import FuelStation
from .dtos import (CoordinateResponse, FuelSummaryResponse, FuelStopResponse,
                         PlanningResponse, RouteResponse, FuelRouteResponse)


MILE_METERS = 1609.344
CAPACITY = 50
MPG = 10
RANGE_METERS = 500 * MILE_METERS
GRID = 10  # tenths of a gallon
US_STATES = set('AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split())


class PlanningUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Candidate:
    station: FuelStation
    marker: float

    @property
    def point(self):
        return CoordinateResponse(float(self.station.latitude), float(self.station.longitude))


def _distance(a, b):
    """Haversine lower bound in miles, used only for candidate ranking."""
    a_lat, a_lng = math.radians(a[1]), math.radians(a[0])
    b_lat, b_lng = math.radians(b[1]), math.radians(b[0])
    h = math.sin((b_lat-a_lat)/2)**2 + math.cos(a_lat)*math.cos(b_lat)*math.sin((b_lng-a_lng)/2)**2
    return 3958.7613 * 2 * math.asin(min(1, math.sqrt(h)))


def select_candidates(candidates, route_miles, max_candidates=23):
    """Keep route coverage within one 25-point Matrix request."""
    if not candidates:
        return []
    bucket_miles = max(100, route_miles / max_candidates)
    buckets = {}
    for candidate in candidates:
        bucket = min(max_candidates - 1, int(candidate.marker // bucket_miles))
        buckets.setdefault(bucket, []).append(candidate)
    chosen = []
    for bucket in sorted(buckets):
        chosen.extend(sorted(buckets[bucket], key=lambda c: (c.station.retail_price, c.marker))[:2])
    if len(chosen) > max_candidates:
        chosen = [min(buckets[b], key=lambda c: (c.station.retail_price, c.marker)) for b in sorted(buckets)]
    remaining = sorted((c for c in candidates if c not in chosen), key=lambda c: c.station.retail_price)
    chosen.extend(remaining[:max_candidates-len(chosen)])
    return sorted(chosen, key=lambda c: c.marker)


def find_candidates(geometry, route_miles, corridor_miles=25, max_candidates=23):
    coordinates = geometry['coordinates']
    # Simplifying only the lookup line keeps the indexed spatial query cheap.
    step = max(1, len(coordinates) // 500)
    sampled = coordinates[::step]
    if sampled[-1] != coordinates[-1]:
        sampled.append(coordinates[-1])
    line = LineString(*[(float(lng), float(lat)) for lng, lat in sampled], srid=4326)
    rows = FuelStation.objects.filter(
        location__isnull=False, latitude__isnull=False, longitude__isnull=False,
        retail_price__gt=0, state__in=US_STATES,
        location__dwithin=(line, D(mi=corridor_miles)),
    ).only('id', 'source_file', 'source_row_number', 'name',
           'address', 'city', 'state', 'retail_price', 'latitude', 'longitude')
    cumulative = [0.0]
    for a, b in zip(sampled, sampled[1:]):
        cumulative.append(cumulative[-1] + _distance(a, b))
    candidates = []
    for row in rows:
        point = (float(row.longitude), float(row.latitude))
        index = min(range(len(sampled)), key=lambda i: _distance(point, sampled[i]))
        marker = route_miles * cumulative[index] / max(cumulative[-1], 0.001)
        if 1 < marker < route_miles - 1:
            candidates.append(Candidate(row, marker))
    return select_candidates(candidates, route_miles, max_candidates), len(candidates)


def optimize_path(candidates, matrix):
    """Dynamic program over 0.1-gallon fuel states on a forward station graph."""
    count = len(candidates) + 2
    finish = count - 1
    # At each node: fuel units -> (cost, driving meters, stop count, path indices).
    arrivals = [dict() for _ in range(count)]
    arrivals[0][500] = (Decimal('0'), 0.0, 0, ())
    for origin in range(count - 1):
        if not arrivals[origin]:
            continue
        if origin == 0:
            departures = arrivals[origin]
        else:
            price = candidates[origin - 1].station.retail_price
            departures = {}
            best = None
            for fuel in range(501):
                state = arrivals[origin].get(fuel)
                if state is not None:
                    key = (state[0] - Decimal(fuel) * price / GRID, state[1], state[2], state[3])
                    if best is None or key < best[0]:
                        best = (key, fuel, state)
                if best is not None:
                    _, initial, prior = best
                    departures[fuel] = (prior[0] + Decimal(fuel-initial)*price/GRID,
                                        prior[1], prior[2], prior[3] + (origin,))
        for dest in range(origin + 1, count):
            road_meters = matrix[origin][dest]
            if road_meters is None or not isinstance(road_meters, (int, float)) or not math.isfinite(road_meters):
                continue
            if road_meters < 0 or road_meters > RANGE_METERS:
                continue
            # Round consumption upward to avoid an optimistic fuel state.
            used = math.ceil(road_meters / MILE_METERS - 1e-9)
            for departure_fuel in range(used, 501):
                state = departures.get(departure_fuel)
                if state is None:
                    continue
                arrival_fuel = departure_fuel - used
                proposal = (state[0], round(state[1] + road_meters, 2),
                            state[2] + (dest != finish), state[3])
                current = arrivals[dest].get(arrival_fuel)
                if current is None or proposal < current:
                    arrivals[dest][arrival_fuel] = proposal
    if not arrivals[finish]:
        raise PlanningUnavailable('No reachable fuel-stop chain in the searched corridor')
    best = min(arrivals[finish].values())
    return [index - 1 for index in best[3]]


def exact_purchases(stations, leg_meters):
    """For a fixed station order, buy only enough to reach cheaper fuel or finish."""
    if any(leg < 0 or leg > RANGE_METERS for leg in leg_meters):
        raise PlanningUnavailable('A final route leg exceeds vehicle range')
    distances = [Decimal(str(meters)) / Decimal(str(MILE_METERS * MPG)) for meters in leg_meters]
    fuel = Decimal(CAPACITY)
    purchases = []
    for index, station in enumerate(stations):
        fuel -= distances[index]
        if fuel < 0:
            raise PlanningUnavailable('The final route cannot be driven with available fuel')
        distance_to_target = Decimal('0')
        for next_index in range(index + 1, len(stations) + 1):
            distance_to_target += distances[next_index]
            if distance_to_target > CAPACITY:
                distance_to_target = Decimal(CAPACITY)
                break
            if next_index == len(stations) or stations[next_index].retail_price < station.retail_price:
                break
        purchase = max(Decimal('0'), distance_to_target - fuel)
        if fuel + purchase > CAPACITY:
            raise PlanningUnavailable('Fuel plan exceeds tank capacity')
        fuel += purchase
        purchases.append(purchase)
    fuel -= distances[-1]
    if fuel < Decimal('-0.0000001'):
        raise PlanningUnavailable('The final route cannot be driven with available fuel')
    return purchases


def remove_unused_stops(selected_indices, candidates, matrix):
    """Drop zero-purchase Matrix waypoints when direct road legs stay feasible."""
    nodes = [0] + [index + 1 for index in selected_indices] + [len(candidates) + 1]

    def purchases_for(path):
        legs = []
        for origin, destination in zip(path, path[1:]):
            distance = matrix[origin][destination]
            if (distance is None or not isinstance(distance, (int, float))
                    or not math.isfinite(distance) or distance < 0):
                raise PlanningUnavailable('A road leg is unavailable')
            legs.append(distance)
        stations = [candidates[node - 1].station for node in path[1:-1]]
        purchases = exact_purchases(stations, legs)
        cost = sum((quantity * station.retail_price for quantity, station in zip(purchases, stations)), Decimal('0'))
        return purchases, cost, sum(legs)

    while True:
        purchases, current_cost, current_distance = purchases_for(nodes)
        removed = False
        for position in range(len(purchases) - 1, -1, -1):
            if purchases[position] != 0:
                continue
            trial = nodes[:position + 1] + nodes[position + 2:]
            try:
                _, trial_cost, trial_distance = purchases_for(trial)
            except PlanningUnavailable:
                continue
            if trial_cost > current_cost or (trial_cost == current_cost and trial_distance > current_distance):
                continue
            nodes = trial
            removed = True
            break
        if not removed:
            return [node - 1 for node in nodes[1:-1]]


def build_response(route, selected, candidate_count, provider_calls):
    stations = [candidate.station for candidate in selected]
    purchases = exact_purchases(stations, route['legs'])
    stops = []
    marker = Decimal('0')
    total = Decimal('0')
    for index, (candidate, purchase) in enumerate(zip(selected, purchases)):
        row = candidate.station
        marker += Decimal(str(route['legs'][index])) / Decimal(str(MILE_METERS))
        if purchase == 0:
            continue
        cost = (purchase * row.retail_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        total += cost
        stops.append(FuelStopResponse(
            station_id=row.id,
            source_row_number=row.source_row_number, name=row.name,
            address=f'{row.address}, {row.city}, {row.state}', location=candidate.point,
            mile_marker=round(float(marker), 2), price_per_gallon_usd=str(row.retail_price),
            gallons_purchased=str(purchase.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)),
            cost_usd=str(cost),
        ))
    distance_miles = route['distance'] / MILE_METERS
    return FuelRouteResponse(
        route=RouteResponse(route['geometry'], round(distance_miles, 2), route['duration']),
        fuel_stops=stops,
        fuel=FuelSummaryResponse(CAPACITY, MPG, CAPACITY,
                     str(sum(purchases, Decimal('0')).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)),
                     str((Decimal(str(route['distance'])) / Decimal(str(MILE_METERS * MPG))).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)),
                     str(total.quantize(Decimal('0.01')))),
        planning=PlanningResponse('best_within_search', 'stations near the direct driving corridor',
                             'LocationIQ', candidate_count, provider_calls),
    )


def plan(request, client):
    if not client.is_us(request.start) or not client.is_us(request.finish):
        raise ValueError('start and finish must be near the contiguous United States')
    direct = client.directions([request.start, request.finish])
    if direct['distance'] <= RANGE_METERS:
        return build_response(direct, [], 0, client.calls)
    candidates, candidate_count = find_candidates(direct['geometry'], direct['distance'] / MILE_METERS)
    if not candidates:
        raise PlanningUnavailable('No fuel stations found in the searched corridor')
    points = [request.start] + [candidate.point for candidate in candidates] + [request.finish]
    matrix = client.matrix(points)
    selected_indices = optimize_path(candidates, matrix)
    selected_indices = remove_unused_stops(selected_indices, candidates, matrix)
    selected = [candidates[index] for index in selected_indices]
    final = client.directions([request.start] + [candidate.point for candidate in selected] + [request.finish])
    if final['distance'] > direct['distance'] * 1.20:
        raise PlanningUnavailable('The selected fuel route exceeds the detour limit')
    # Directions legs, not Matrix estimates, are authoritative.
    return build_response(final, selected, candidate_count, client.calls)
