"""Thirty fixed route scenarios; no CSV or live LocationIQ access."""

from decimal import Decimal
from unittest.mock import Mock

from django.test import SimpleTestCase

from .clients.routing import RoutingClient
from .dtos import CoordinateRequest, FuelRouteRequest, RequestValidationError
from .planner import (
    Candidate, MILE_METERS, PlanningUnavailable, build_response,
    exact_purchases, optimize_path,
)


def request_body():
    return {
        'start': {'lat': 41.8781, 'lng': -87.6298},
        'finish': {'lat': 39.7392, 'lng': -104.9903},
    }


def station(price, identifier):
    return Mock(
        id=identifier, source_file='test-fixture', source_row_number=identifier,
        name=f'Station {identifier}', address='1 Test St', city='Test City',
        state='CO', latitude=Decimal('40'), longitude=Decimal('-100'),
        retail_price=Decimal(price),
    )


def miles(*values):
    return [value * MILE_METERS for value in values]


class RequestCases(SimpleTestCase):
    def assert_invalid(self, body):
        with self.assertRaises(RequestValidationError):
            FuelRouteRequest.parse(body)

    def test_01_missing_start(self):
        body = request_body()
        del body['start']
        self.assert_invalid(body)

    def test_02_missing_finish(self):
        body = request_body()
        del body['finish']
        self.assert_invalid(body)

    def test_03_extra_top_level_field(self):
        body = request_body()
        body['vehicle'] = 'truck'
        self.assert_invalid(body)

    def test_04_start_address_instead_of_coordinates(self):
        body = request_body()
        body['start'] = {'address': 'Chicago, IL'}
        self.assert_invalid(body)

    def test_05_finish_address_instead_of_coordinates(self):
        body = request_body()
        body['finish'] = {'address': 'Denver, CO'}
        self.assert_invalid(body)

    def test_06_missing_start_lat(self):
        body = request_body()
        del body['start']['lat']
        self.assert_invalid(body)

    def test_07_missing_finish_lng(self):
        body = request_body()
        del body['finish']['lng']
        self.assert_invalid(body)

    def test_08_extra_coordinate_field(self):
        body = request_body()
        body['start']['altitude'] = 100
        self.assert_invalid(body)

    def test_09_boolean_latitude(self):
        body = request_body()
        body['start']['lat'] = True
        self.assert_invalid(body)

    def test_10_string_longitude(self):
        body = request_body()
        body['start']['lng'] = '-87.6298'
        self.assert_invalid(body)

    def test_11_latitude_over_90(self):
        body = request_body()
        body['start']['lat'] = 90.01
        self.assert_invalid(body)

    def test_12_longitude_below_minus_180(self):
        body = request_body()
        body['finish']['lng'] = -180.01
        self.assert_invalid(body)

    def test_13_same_start_and_finish(self):
        body = request_body()
        body['finish'] = body['start'].copy()
        self.assert_invalid(body)

    def test_14_malformed_json(self):
        with self.assertRaises(RequestValidationError):
            FuelRouteRequest.from_json(b'{')


class RadiusCases(SimpleTestCase):
    def setUp(self):
        self.session = Mock()
        self.client = RoutingClient('test-key', session=self.session)

    def tearDown(self):
        self.session.get.assert_not_called()
        self.assertEqual(self.client.calls, 0)

    def test_15_kansas_center_is_inside(self):
        self.assertTrue(self.client.is_us(CoordinateRequest(39.833333, -98.583333)))

    def test_16_chicago_is_inside(self):
        self.assertTrue(self.client.is_us(CoordinateRequest(41.8781, -87.6298)))

    def test_17_eastern_maine_is_inside(self):
        self.assertTrue(self.client.is_us(CoordinateRequest(44.906, -66.99)))

    def test_18_london_is_outside(self):
        self.assertFalse(self.client.is_us(CoordinateRequest(51.5072, -0.1276)))

    def test_19_honolulu_is_outside_current_radius(self):
        self.assertFalse(self.client.is_us(CoordinateRequest(21.3099, -157.8581)))

    def test_20_anchorage_is_outside_current_radius(self):
        self.assertFalse(self.client.is_us(CoordinateRequest(61.2181, -149.9003)))


class FuelCases(SimpleTestCase):
    def test_21_short_trip_needs_no_purchase(self):
        self.assertEqual(exact_purchases([], miles(100)), [])

    def test_22_exactly_500_miles_needs_no_purchase(self):
        self.assertEqual(exact_purchases([], miles(500)), [])

    def test_23_501_miles_without_station_is_unreachable(self):
        with self.assertRaises(PlanningUnavailable):
            exact_purchases([], miles(501))

    def test_24_one_stop_after_400_miles_buys_10_gallons(self):
        self.assertEqual(exact_purchases([station('3.00', 1)], miles(400, 200)), [Decimal('10')])

    def test_25_empty_tank_at_stop_buys_20_gallons(self):
        self.assertEqual(exact_purchases([station('3.00', 1)], miles(500, 200)), [Decimal('20')])

    def test_26_cheaper_later_stop_gets_more_fuel(self):
        purchases = exact_purchases([station('4.00', 1), station('3.00', 2)], miles(500, 100, 200))
        self.assertEqual(purchases, [Decimal('10'), Decimal('20')])

    def test_27_cheaper_early_stop_fills_for_later_leg(self):
        purchases = exact_purchases([station('3.00', 1), station('4.00', 2)], miles(400, 200, 200))
        self.assertEqual(purchases, [Decimal('30'), Decimal('0')])

    def test_28_leg_over_500_miles_is_rejected(self):
        with self.assertRaises(PlanningUnavailable):
            exact_purchases([station('3.00', 1)], miles(100, 501))

    def test_29_stop_cost_rounds_half_up_to_cents(self):
        legs = miles(500, 10)
        route = {
            'geometry': {'type': 'LineString', 'coordinates': [[-100, 40], [-101, 40]]},
            'distance': sum(legs), 'duration': 3600, 'legs': legs,
        }
        response = build_response(route, [Candidate(station('3.335', 1), 500)], 1, 3)
        self.assertEqual(response.fuel_stops[0].cost_usd, '3.34')
        self.assertEqual(response.fuel.total_fuel_cost_usd, '3.34')

    def test_30_matrix_prefers_cheaper_reachable_stop(self):
        candidates = [Candidate(station('4.00', 1), 400), Candidate(station('3.00', 2), 450)]
        matrix = [[value * MILE_METERS if value is not None else None for value in row] for row in [
            [0, 400, 450, 800], [None, 0, 50, 400],
            [None, None, 0, 350], [None, None, None, 0],
        ]]
        self.assertEqual(optimize_path(candidates, matrix), [1])
