"""Check direct, via-stop Directions and Matrix without logging the API key.

Run with ``.venv/bin/python scripts/locationiq_routing_poc.py``. This is a
manual smoke test, not part of the automated test suite.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env")
API_KEY = os.getenv("LOCATIONIQ_API_KEY", "")
BASE_URL = "https://us1.locationiq.com/v1"
POINTS = [
    (-87.6298, 41.8781),  # Chicago
    (-95.9980, 41.2565),  # Omaha
    (-104.9903, 39.7392),  # Denver
]


def request(service, points, extra):
    path = ";".join(f"{longitude},{latitude}" for longitude, latitude in points)
    url = f"{BASE_URL}/{service}/driving/{path}"
    params = {"key": API_KEY, **extra}
    started = time.perf_counter()
    try:
        response = requests.get(url, params=params, timeout=30)
    except requests.RequestException:
        raise RuntimeError(f"{service} request failed; URL and key withheld") from None
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(f"{service} returned HTTP {response.status_code} without JSON") from None
    if response.status_code != 200 or payload.get("code") != "Ok":
        code = payload.get("code", "unknown") if isinstance(payload, dict) else "unknown"
        raise RuntimeError(f"{service} returned HTTP {response.status_code}, code {code}")
    return payload, elapsed_ms


def main():
    if not API_KEY:
        raise RuntimeError("LOCATIONIQ_API_KEY is not configured")

    directions, directions_ms = request(
        "directions", [POINTS[0], POINTS[2]],
        {"geometries": "geojson", "overview": "full", "steps": "false"},
    )
    route = directions["routes"][0]
    if route["geometry"]["type"] != "LineString" or len(route["legs"]) != 1:
        raise RuntimeError("Directions returned an unexpected route shape")

    matrix, matrix_ms = request(
        "matrix", POINTS, {"annotations": "distance,duration"},
    )
    distances = matrix["distances"]
    durations = matrix["durations"]
    if len(distances) != 3 or len(durations) != 3:
        raise RuntimeError("Matrix returned an unexpected table shape")

    via_directions, via_ms = request(
        "directions", POINTS,
        {"geometries": "geojson", "overview": "full", "steps": "false"},
    )
    via_route = via_directions["routes"][0]
    if len(via_route["legs"]) != 2 or via_route["geometry"]["type"] != "LineString":
        raise RuntimeError("Via-stop Directions returned an unexpected route shape")
    via_leg_meters = [leg["distance"] for leg in via_route["legs"]]
    if abs(sum(via_leg_meters) - via_route["distance"]) > 1:
        raise RuntimeError("Via-stop legs do not add up to the route distance")

    print(json.dumps({
        "directions": {
            "distance_meters": route["distance"],
            "duration_seconds": route["duration"],
            "geometry_points": len(route["geometry"]["coordinates"]),
            "elapsed_ms": directions_ms,
        },
        "matrix": {
            "shape": "3x3",
            "chicago_to_omaha_meters": distances[0][1],
            "omaha_to_denver_meters": distances[1][2],
            "chicago_to_denver_meters": distances[0][2],
            "chicago_to_denver_seconds": durations[0][2],
            "elapsed_ms": matrix_ms,
        },
        "via_stop_directions": {
            "leg_distances_meters": via_leg_meters,
            "total_distance_meters": via_route["distance"],
            "geometry_points": len(via_route["geometry"]["coordinates"]),
            "elapsed_ms": via_ms,
        },
        "external_requests": 3,
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, IndexError, TypeError) as error:
        print(f"Routing smoke test failed: {error}", file=sys.stderr)
        sys.exit(1)
