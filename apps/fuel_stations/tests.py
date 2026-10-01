import csv
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import TestCase

from apps.geo.clients.locationiq import GeocodingResult
from apps.geo.models import NominatimSearchCache

from .management.commands.retry_fuel_stations_osm import PacedNominatimClient
from .models import FuelStation
from .services import geocode_stations, retry_unmatched_stations, stage_csv


class FuelStationImportTests(TestCase):
    def test_duplicate_truckstop_rows_are_preserved_and_geocoded_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'stations.csv'
            with path.open('w', newline='', encoding='utf-8') as output:
                writer = csv.writer(output)
                writer.writerow([
                    'OPIS Truckstop ID', 'Truckstop Name', 'Address', 'City',
                    'State', 'Rack ID', 'Retail Price',
                ])
                writer.writerow(['7', 'Station', '1 Main St', 'Albany', 'NY', '20', '3.10'])
                writer.writerow(['7', 'Station', '1 Main St', 'Albany', 'NY', '20', '3.20'])

            source_file, staged = stage_csv(path, batch_size=1)
            client = Mock()
            client.forward_geocode.return_value = GeocodingResult(
                Decimal('42.65258'), Decimal('-73.75623'), '1 Main St, Albany, NY'
            )
            geocoded, no_match = geocode_stations(source_file, client, batch_size=1)

            self.assertEqual((staged, geocoded, no_match), (2, 2, 0))
            self.assertEqual(client.forward_geocode.call_count, 1)
            self.assertEqual(FuelStation.objects.count(), 2)
            self.assertEqual(
                list(FuelStation.objects.values_list('retail_price', flat=True)),
                [Decimal('3.10000000'), Decimal('3.20000000')],
            )
            station = FuelStation.objects.first()
            self.assertEqual(station.display_name, '1 Main St, Albany, NY')
            self.assertEqual(station.location.x, float(station.longitude))
            self.assertEqual(station.location.y, float(station.latitude))

            stage_csv(path, batch_size=2)
            self.assertEqual(geocode_stations(source_file, client, batch_size=2), (0, 0))
            self.assertEqual(client.forward_geocode.call_count, 1)

    def test_falls_back_to_station_name_after_address_has_no_match(self):
        station = FuelStation.objects.create(
            source_file='test.csv', source_row_number=2, opis_truckstop_id='9',
            name='Kwik Trip', address='I-94 exit 143', city='Tomah', state='WI',
            rack_id='20', retail_price=Decimal('3.20'),
        )
        client = Mock()
        client.forward_geocode.side_effect = [
            None,
            GeocodingResult(Decimal('43.98'), Decimal('-90.50'), 'Kwik Trip, Tomah'),
        ]

        self.assertEqual(geocode_stations('test.csv', client, batch_size=1), (1, 0))
        self.assertEqual(client.forward_geocode.call_count, 2)
        self.assertIn('Kwik Trip', client.forward_geocode.call_args.args[0])
        station.refresh_from_db()
        self.assertEqual(station.display_name, 'Kwik Trip, Tomah')


class FuelStationOSMTests(TestCase):
    @patch('apps.fuel_stations.management.commands.retry_fuel_stations_osm.time.sleep')
    @patch('apps.fuel_stations.management.commands.retry_fuel_stations_osm.time.monotonic')
    def test_command_sleeps_between_searches(self, monotonic, sleep):
        monotonic.side_effect = [100, 101, 116]
        client = Mock()
        paced = PacedNominatimClient(client, 16)

        paced.forward_geocode('address')
        paced.forward_geocode('name')

        sleep.assert_called_once_with(15)
        self.assertEqual(client.forward_geocode.call_count, 2)

    def test_osm_retry_updates_only_unresolved_rows_and_caches_queries(self):
        stations = []
        for number, status in enumerate(['no_match', 'error', 'pending'], start=2):
            stations.append(FuelStation.objects.create(
                source_file='test.csv', source_row_number=number, opis_truckstop_id=str(number),
                name='Station', address='1 Main St', city='Albany', state='NY',
                rack_id='20', retail_price=Decimal('3.20'), geocoding_status=status,
            ))
        client = Mock()
        client.forward_geocode.return_value = GeocodingResult(
            Decimal('42.65258'), Decimal('-73.75623'), '1 Main St, Albany, NY'
        )

        self.assertEqual(retry_unmatched_stations(client, batch_size=1), (2, 0))
        self.assertEqual(client.forward_geocode.call_count, 1)
        self.assertEqual(NominatimSearchCache.objects.count(), 1)
        for station in stations[:2]:
            station.refresh_from_db()
            self.assertEqual(station.geocoding_status, FuelStation.GeocodingStatus.GEOCODED)
            self.assertIsNotNone(station.osm_attempted_at)
            self.assertEqual(station.location.x, float(station.longitude))
        stations[2].refresh_from_db()
        self.assertEqual(stations[2].geocoding_status, FuelStation.GeocodingStatus.PENDING)
        self.assertEqual(retry_unmatched_stations(client, batch_size=1), (0, 0))

    def test_osm_no_match_is_not_retried_on_next_run(self):
        station = FuelStation.objects.create(
            source_file='test.csv', source_row_number=2, opis_truckstop_id='2',
            name='Station', address='Unknown road', city='Albany', state='NY',
            rack_id='20', retail_price=Decimal('3.20'), geocoding_status='error',
        )
        client = Mock()
        client.forward_geocode.return_value = None
        self.assertEqual(retry_unmatched_stations(client, batch_size=1), (0, 1))
        self.assertEqual(client.forward_geocode.call_count, 2)
        station.refresh_from_db()
        self.assertEqual(station.geocoding_status, FuelStation.GeocodingStatus.NO_MATCH)
        self.assertIsNotNone(station.osm_attempted_at)
        self.assertEqual(retry_unmatched_stations(client, batch_size=1), (0, 0))
        self.assertEqual(client.forward_geocode.call_count, 2)
