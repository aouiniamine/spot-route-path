import json
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .dtos import CoordinateRequest, RequestValidationError, FuelRouteRequest
from .planner import Candidate, PlanningUnavailable, build_response, exact_purchases, optimize_path, plan
from .clients.routing import ProviderError, RoutingClient


def station(price, identifier):
    return Mock(id=identifier, source_row_number=identifier, name=f'Station {identifier}',
                source_file='test.csv', address='Main St', city='Somewhere', state='CO',
                latitude=Decimal('40'), longitude=Decimal('-100'),
                retail_price=Decimal(price))


def route(legs):
    return {'geometry': {'type': 'LineString', 'coordinates': [[-100, 40], [-101, 40]]},
            'distance': sum(legs), 'duration': 3600, 'legs': legs}


class DTOTests(TestCase):
    def test_endpoint_path(self):
        self.assertEqual(reverse('fuel-plan'), '/api/geo/route')

    def test_request_is_coordinates_only(self):
        data = {'start': {'lat': 41.88, 'lng': -87.63},
                'finish': {'lat': 39.74, 'lng': -104.99}}
        self.assertEqual(FuelRouteRequest.parse(data).start.lat, 41.88)
        with self.assertRaises(RequestValidationError):
            FuelRouteRequest.parse({'start': {'address': 'Chicago'}, 'finish': data['finish']})
        with self.assertRaises(RequestValidationError):
            FuelRouteRequest.parse({'start': {'lat': True, 'lng': -87.63}, 'finish': data['finish']})

    def test_endpoint_rejects_bad_input(self):
        response = self.client.post(reverse('fuel-plan'), data=json.dumps({
            'start': {'address': 'Chicago'}, 'finish': {'lat': 40, 'lng': -105},
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'invalid_request')

    def test_endpoint_rejects_malformed_json(self):
        response = self.client.post(reverse('fuel-plan'), data='{', content_type='text/plain')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['message'], 'Body must be valid JSON')

    @patch('apps.geo.views.plan')
    @patch('apps.geo.views.RoutingClient')
    def test_endpoint_returns_typed_response(self, client, planner):
        client.return_value.calls = 1
        planner.return_value = build_response(route([100_000]), [], 0, 1)
        response = self.client.post(reverse('fuel-plan'), data=json.dumps({
            'start': {'lat': 41.88, 'lng': -87.63},
            'finish': {'lat': 39.74, 'lng': -104.99},
        }), content_type='text/plain')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['fuel']['total_fuel_cost_usd'], '0.00')
        self.assertEqual(response.json()['route']['geometry']['type'], 'LineString')


class FuelMathTests(SimpleTestCase):
    def test_cheaper_later_station_gets_partial_first_fill(self):
        # Start tank reaches stop 1 empty. Buy enough for stop 2, then finish.
        stations = [station('4.00', 1), station('3.00', 2)]
        meters = [500, 100, 200]
        from .planner import MILE_METERS
        purchases = exact_purchases(stations, [distance * MILE_METERS for distance in meters])
        self.assertEqual([round(float(x), 4) for x in purchases], [10.0, 20.0])

    def test_cheaper_early_station_fills_tank(self):
        from .planner import MILE_METERS
        stations = [station('3.00', 1), station('4.00', 2)]
        purchases = exact_purchases(stations, [400 * MILE_METERS, 200 * MILE_METERS, 200 * MILE_METERS])
        self.assertEqual([round(float(x), 4) for x in purchases], [30.0, 0.0])

    def test_final_leg_range_is_enforced(self):
        from .planner import MILE_METERS
        with self.assertRaises(PlanningUnavailable):
            exact_purchases([station('3.00', 1)], [100 * MILE_METERS, 501 * MILE_METERS])

    def test_matrix_path_prefers_lower_cost(self):
        candidates = [Candidate(station('4.00', 1), 400), Candidate(station('3.00', 2), 450)]
        from .planner import MILE_METERS
        def miles(value):
            return value * MILE_METERS if value is not None else None
        matrix = [[miles(x) for x in row] for row in [
            [0, 400, 450, 800], [None, 0, 50, 400],
            [None, None, 0, 350], [None, None, None, 0],
        ]]
        self.assertEqual(optimize_path(candidates, matrix), [1])

    @patch('apps.geo.planner.optimize_path', return_value=[0, 1, 2])
    @patch('apps.geo.planner.find_candidates')
    def test_zero_purchase_waypoint_is_removed_from_final_route(self, find, optimize):
        from .planner import MILE_METERS
        candidates = [Candidate(station('3.10', 1), 400),
                      Candidate(station('3.20', 2), 410),
                      Candidate(station('2.90', 3), 550)]
        find.return_value = (candidates, 3)
        provider = Mock(calls=3)
        provider.is_us.return_value = True
        provider.matrix.return_value = [
            [0, 400 * MILE_METERS, None, None, 900 * MILE_METERS],
            [None, 0, 10 * MILE_METERS, 150 * MILE_METERS, None],
            [None, None, 0, 140 * MILE_METERS, None],
            [None, None, None, 0, 450 * MILE_METERS],
            [None, None, None, None, 0],
        ]
        provider.directions.side_effect = [
            route([900 * MILE_METERS]),
            route([400 * MILE_METERS, 150 * MILE_METERS, 450 * MILE_METERS]),
        ]
        result = plan(FuelRouteRequest.parse({
            'start': {'lat': 41, 'lng': -87}, 'finish': {'lat': 39, 'lng': -105},
        }), provider)
        self.assertEqual([stop.station_id for stop in result.fuel_stops], [1, 3])
        self.assertTrue(all(Decimal(stop.gallons_purchased) > 0 for stop in result.fuel_stops))
        self.assertEqual(provider.directions.call_count, 2)

    def test_final_distance_change_does_not_display_zero_purchase_stop(self):
        from .planner import MILE_METERS
        selected = [Candidate(station('3.20', 1), 400),
                    Candidate(station('3.10', 2), 410),
                    Candidate(station('2.90', 3), 490)]
        result = build_response(route([400 * MILE_METERS, 10 * MILE_METERS,
                                       80 * MILE_METERS, 450 * MILE_METERS]),
                                selected, 3, 3)
        self.assertEqual([stop.station_id for stop in result.fuel_stops], [3])
        self.assertEqual(result.fuel_stops[0].mile_marker, 490)


class ProviderTests(SimpleTestCase):
    def test_us_radius_uses_no_provider_request(self):
        session = Mock()
        client = RoutingClient('test-key', session=session)
        self.assertTrue(client.is_us(CoordinateRequest(39.833333, -98.583333)))
        self.assertTrue(client.is_us(CoordinateRequest(44.906, -66.99)))
        self.assertFalse(client.is_us(CoordinateRequest(51.5072, -0.1276)))
        self.assertEqual(client.calls, 0)
        session.get.assert_not_called()

    def test_provider_error_does_not_expose_key(self):
        session = Mock()
        # requests errors are caught, and secrets stay out of the public error.
        import requests
        session.get.side_effect = requests.Timeout('https://example/?key=secret')
        client = RoutingClient('secret', session=session)
        with self.assertRaises(ProviderError) as failure:
            client.directions([CoordinateRequest(40, -100), CoordinateRequest(39, -101)])
        self.assertNotIn('secret', str(failure.exception))
