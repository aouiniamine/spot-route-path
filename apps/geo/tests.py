from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .clients.locationiq import LocationIQClient, LocationIQError
from .clients.nominatim import NominatimClient, NominatimError


class LocationIQClientTests(SimpleTestCase):
    def test_forward_geocode_reads_coordinates_and_display_name(self):
        session = Mock()
        session.get.return_value = Mock(
            status_code=200,
            json=lambda: [{'lat': '40.12345678', 'lon': '-74.87654321', 'display_name': 'A place'}],
        )
        client = LocationIQClient('test-key', session=session, request_interval=0)

        result = client.forward_geocode('Main Street, NY', country_code='us')

        self.assertEqual(str(result.latitude), '40.12345678')
        self.assertEqual(str(result.longitude), '-74.87654321')
        self.assertEqual(result.display_name, 'A place')
        self.assertEqual(session.get.call_args.kwargs['params']['format'], 'json')
        self.assertEqual(session.get.call_args.kwargs['params']['countrycodes'], 'us')

    def test_404_is_no_match(self):
        session = Mock()
        session.get.return_value = Mock(status_code=404)
        client = LocationIQClient('test-key', session=session, request_interval=0)
        self.assertIsNone(client.forward_geocode('Unknown address'))

    def test_skips_results_outside_expected_city_and_state(self):
        session = Mock()
        session.get.return_value = Mock(
            status_code=200,
            json=lambda: [
                {
                    'lat': '36.1', 'lon': '-95.8', 'display_name': 'Tulsa',
                    'address': {'city': 'Tulsa', 'state_code': 'ok'},
                },
                {
                    'lat': '36.5', 'lon': '-95.2', 'display_name': 'Big Cabin',
                    'address': {'city': 'Big Cabin', 'state_code': 'ok'},
                },
            ],
        )
        client = LocationIQClient('test-key', session=session, request_interval=0)

        result = client.forward_geocode(
            'Highway exit', expected_city='Big Cabin', expected_state='OK'
        )

        self.assertEqual(result.display_name, 'Big Cabin')
        params = session.get.call_args.kwargs['params']
        self.assertEqual(params['addressdetails'], 1)
        self.assertEqual(params['statecode'], 1)

    @patch('apps.geo.clients.locationiq.time.sleep')
    def test_rate_limit_is_retried(self, sleep):
        session = Mock()
        session.get.side_effect = [
            Mock(status_code=429, headers={'Retry-After': '2'}),
            Mock(status_code=200, json=lambda: []),
        ]
        client = LocationIQClient('test-key', session=session, request_interval=0)
        self.assertIsNone(client.forward_geocode('Unknown address'))
        sleep.assert_called_once_with(2.0)

    def test_error_does_not_expose_api_key(self):
        session = Mock()
        session.get.return_value = Mock(status_code=401)
        client = LocationIQClient('private-key', session=session, request_interval=0)
        with self.assertRaises(LocationIQError) as raised:
            client.forward_geocode('Main Street')
        self.assertNotIn('private-key', str(raised.exception))


class NominatimClientTests(SimpleTestCase):
    def test_accepts_matching_town_and_state_and_identifies_app(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, json=lambda: [{
            'lat': '36.5', 'lon': '-95.2', 'display_name': 'Big Cabin, Oklahoma',
            'address': {'town': 'Big Cabin', 'ISO3166-2-lvl4': 'US-OK', 'country_code': 'us'},
        }])
        client = NominatimClient('team@example.com', session=session)
        result = client.forward_geocode(
            'Big Cabin', country_code='us', expected_city='Big Cabin', expected_state='OK'
        )
        self.assertEqual(result.display_name, 'Big Cabin, Oklahoma')
        arguments = session.get.call_args.kwargs
        self.assertEqual(arguments['params']['format'], 'jsonv2')
        self.assertEqual(arguments['params']['email'], 'team@example.com')
        self.assertIn('team@example.com', arguments['headers']['User-Agent'])

    def test_rejects_wrong_state_and_stops_on_rate_limit(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, json=lambda: [{
            'lat': '36.5', 'lon': '-95.2', 'display_name': 'Wrong state',
            'address': {'city': 'Big Cabin', 'ISO3166-2-lvl4': 'US-TX', 'country_code': 'us'},
        }])
        client = NominatimClient('team@example.com', session=session)
        self.assertIsNone(client.forward_geocode(
            'Big Cabin', country_code='us', expected_city='Big Cabin', expected_state='OK'
        ))
        session.get.return_value = Mock(status_code=429)
        with self.assertRaises(NominatimError):
            client.forward_geocode('Big Cabin')
        self.assertEqual(session.get.call_count, 2)

    def test_public_endpoint_requires_contact(self):
        with self.assertRaises(ValueError):
            NominatimClient('')
