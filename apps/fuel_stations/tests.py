import csv
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from django.test import TestCase

from apps.geo.clients.locationiq import GeocodingResult

from .models import FuelStation
from .services import geocode_stations, stage_csv


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
