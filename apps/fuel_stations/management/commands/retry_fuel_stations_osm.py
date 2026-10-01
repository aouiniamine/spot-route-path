import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.fuel_stations.services import retry_unmatched_stations
from apps.geo.clients.nominatim import NominatimClient, NominatimError


PUBLIC_REQUEST_INTERVAL = 1.0


class PacedNominatimClient:
    """Apply command-level pacing to every uncached Nominatim search."""

    def __init__(self, client: NominatimClient, request_interval: float):
        self.client = client
        self.request_interval = request_interval
        self.last_request_at = None

    def forward_geocode(self, *args, **kwargs):
        if self.last_request_at is not None:
            remaining = self.request_interval - (time.monotonic() - self.last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_at = time.monotonic()
        return self.client.forward_geocode(*args, **kwargs)


class Command(BaseCommand):
    help = 'Retry unmatched fuel stations with OpenStreetMap Nominatim.'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=25)
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument(
            '--request-interval', type=float, default=PUBLIC_REQUEST_INTERVAL,
            help='Seconds between Nominatim requests (public API minimum: 1/s).',
        )

    def handle(self, *args, **options):
        if options['batch_size'] <= 0 or options['limit'] <= 0:
            raise CommandError('--batch-size and --limit must be greater than zero')
        try:
            client = NominatimClient(
                settings.NOMINATIM_CONTACT_EMAIL,
                base_url=settings.NOMINATIM_BASE_URL,
            )
            minimum = PUBLIC_REQUEST_INTERVAL if client.is_public else 0
            if options['request_interval'] < minimum:
                raise CommandError(f'--request-interval must be at least {minimum:g} seconds')
            if client.is_public and options['limit'] > 100:
                raise CommandError('Public Nominatim is limited to 100 rows per run; use a private endpoint for larger jobs')
            found, missing = retry_unmatched_stations(
                PacedNominatimClient(client, options['request_interval']),
                batch_size=options['batch_size'], limit=options['limit'],
                progress=lambda found, missing: self.stdout.write(
                    f'Processed: {found} geocoded, {missing} without a match.'
                ),
            )
        except (ValueError, NominatimError) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(self.style.SUCCESS(
            f'Finished: {found} geocoded, {missing} without a match.'
        ))
