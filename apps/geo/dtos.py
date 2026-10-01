"""Public request and response shapes for the fuel route endpoint."""

from dataclasses import asdict, dataclass
import json
import math


class RequestValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CoordinateRequest:
    lat: float
    lng: float

    @classmethod
    def parse(cls, value, field):
        if not isinstance(value, dict) or set(value) != {'lat', 'lng'}:
            raise RequestValidationError(f'{field} must contain only lat and lng')
        if any(isinstance(value[key], bool) or not isinstance(value[key], (int, float))
               for key in ('lat', 'lng')):
            raise RequestValidationError(f'{field} lat and lng must be numbers')
        lat, lng = float(value['lat']), float(value['lng'])
        if not math.isfinite(lat) or not math.isfinite(lng) or not -90 <= lat <= 90 or not -180 <= lng <= 180:
            raise RequestValidationError(f'{field} coordinates are invalid')
        return cls(lat=lat, lng=lng)


@dataclass(frozen=True)
class FuelRouteRequest:
    start: CoordinateRequest
    finish: CoordinateRequest

    @classmethod
    def from_json(cls, body: bytes):
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise RequestValidationError('Body must be valid JSON') from None
        return cls.parse(payload)

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != {'start', 'finish'}:
            raise RequestValidationError('Body must contain only start and finish')
        start = CoordinateRequest.parse(value['start'], 'start')
        finish = CoordinateRequest.parse(value['finish'], 'finish')
        if start == finish:
            raise RequestValidationError('start and finish must differ')
        return cls(start, finish)


@dataclass(frozen=True)
class CoordinateResponse:
    lat: float
    lng: float


@dataclass(frozen=True)
class RouteResponse:
    geometry: dict
    distance_miles: float
    duration_seconds: float


@dataclass(frozen=True)
class FuelStopResponse:
    station_id: int
    source_file: str
    source_row_number: int
    name: str
    address: str
    location: CoordinateResponse
    mile_marker: float
    price_per_gallon_usd: str
    gallons_purchased: str
    cost_usd: str


@dataclass(frozen=True)
class FuelSummaryResponse:
    capacity_gallons: int
    miles_per_gallon: int
    initial_gallons: int
    gallons_purchased: str
    estimated_gallons_consumed: str
    total_fuel_cost_usd: str


@dataclass(frozen=True)
class PlanningResponse:
    status: str
    search_scope: str
    routing_provider: str
    candidate_count: int
    provider_calls: int


@dataclass(frozen=True)
class FuelRouteResponse:
    route: RouteResponse
    fuel_stops: list[FuelStopResponse]
    fuel: FuelSummaryResponse
    planning: PlanningResponse

    def to_dict(self):
        return asdict(self)
