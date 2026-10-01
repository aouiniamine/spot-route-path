from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.geo.clients.locationiq import LocationIQClient, LocationIQError
from apps.geo.cache import invalidate_route_cache
from apps.fuel_stations.services import deduplicate_stations, geocode_stations, stage_csv


class Command(BaseCommand):
    help = 'Import fuel stations from CSV and forward geocode their addresses with LocationIQ.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file', type=Path,
            default=settings.BASE_DIR / 'data' / 'truck-stations-and-prices.csv',
            help='Path to the input CSV.',
        )
        parser.add_argument('--batch-size', type=int, default=100)
        parser.add_argument(
            '--request-interval', type=float, default=1.0,
            help='Minimum seconds between LocationIQ requests (default: 1).',
        )
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Geocode at most this many rows; useful for a small trial run.',
        )
        parser.add_argument(
            '--retry-no-match', action='store_true',
            help='Retry rows that previously had no LocationIQ match.',
        )

    def handle(self, *args, **options):
        path = options['file']
        if not path.is_file():
            raise CommandError(f'CSV file does not exist: {path}')
        if options['batch_size'] <= 0:
            raise CommandError('--batch-size must be greater than zero')
        if options['request_interval'] < 0:
            raise CommandError('--request-interval cannot be negative')
        if options['limit'] is not None and options['limit'] <= 0:
            raise CommandError('--limit must be greater than zero')
        api_key = settings.LOCATIONIQ_API_KEY
        if not api_key:
            raise CommandError('Set LOCATIONIQ_API_KEY before importing')

        try:
            source_file, staged = stage_csv(path, batch_size=options['batch_size'])
            invalidate_route_cache()
            self.stdout.write(f'Staged {staged} CSV rows from {source_file}.')
            groups, removed = deduplicate_stations()
            self.stdout.write(f'Removed {removed} repeated rows from {groups} truckstops.')
            client = LocationIQClient(
                api_key, request_interval=options['request_interval']
            )
            geocoded, no_match = geocode_stations(
                source_file, client,
                batch_size=options['batch_size'],
                retry_no_match=options['retry_no_match'],
                limit=options['limit'],
                progress=lambda found, missing: self.stdout.write(
                    f'Processed: {found} geocoded, {missing} without a match.'
                ),
            )
            invalidate_route_cache()
        except (OSError, ValueError, LocationIQError) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(self.style.SUCCESS(
            f'Finished: {geocoded} geocoded, {no_match} without a match.'
        ))
