from django.core.management.base import BaseCommand

from apps.fuel_stations.services import deduplicate_stations
from apps.geo.cache import invalidate_route_cache


class Command(BaseCommand):
    help = 'Keep the lowest-price row per physical truckstop and remove other price rows.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Report duplicates without changing rows.')

    def handle(self, *args, **options):
        groups, removed = deduplicate_stations(dry_run=options['dry_run'])
        if removed and not options['dry_run']:
            invalidate_route_cache()
        verb = 'Would remove' if options['dry_run'] else 'Removed'
        self.stdout.write(f'{verb} {removed} rows from {groups} duplicate truckstop groups.')
