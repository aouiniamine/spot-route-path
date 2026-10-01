"""CSV staging and batched geocoding for fuel stations."""

import csv
import hashlib
import json
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import Path
from typing import Callable

from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import connection, transaction
from django.utils import timezone

from apps.geo.clients.nominatim import NominatimError
from apps.geo.clients.locationiq import GeocodingResult, LocationIQClient, LocationIQError
from apps.geo.models import NominatimSearchCache

from .models import FuelStation
from .selectors import stations_needing_geocoding


CSV_COLUMNS = {
    'OPIS Truckstop ID': 'opis_truckstop_id',
    'Truckstop Name': 'name',
    'Address': 'address',
    'City': 'city',
    'State': 'state',
    'Rack ID': 'rack_id',
    'Retail Price': 'retail_price',
}
SOURCE_FIELDS = tuple(CSV_COLUMNS.values())
GEOCODING_FIELDS = (
    'latitude', 'longitude', 'location', 'display_name',
    'geocoding_status', 'geocoding_error',
    'osm_attempted_at',
)
CANADIAN_PROVINCES = {'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE', 'QC', 'SK', 'YT'}


def source_name(path: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(settings.BASE_DIR))
    except ValueError:
        return str(path)


def stage_csv(path: Path, *, batch_size: int) -> tuple[str, int]:
    """Upsert every CSV row, retaining geocodes when its address has not changed."""
    source_file = source_name(path)
    count = 0
    with path.open('r', newline='', encoding='utf-8-sig') as csv_file:
        reader = csv.DictReader(csv_file)
        missing = set(CSV_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f'CSV is missing columns: {", ".join(sorted(missing))}')

        batch = []
        for row_number, row in enumerate(reader, start=2):
            data = {field: (row[column] or '').strip() for column, field in CSV_COLUMNS.items()}
            if any(not value for value in data.values()):
                raise ValueError(f'CSV row {row_number} has a blank required value')
            try:
                data['retail_price'] = Decimal(data['retail_price'])
            except InvalidOperation:
                raise ValueError(f'CSV row {row_number} has an invalid retail price') from None
            if not data['retail_price'].is_finite():
                raise ValueError(f'CSV row {row_number} has an invalid retail price')
            batch.append(FuelStation(source_file=source_file, source_row_number=row_number, **data))
            count += 1
            if len(batch) >= batch_size:
                _save_source_batch(batch)
                batch.clear()
        if batch:
            _save_source_batch(batch)
    return source_file, count


@transaction.atomic
def deduplicate_stations(*, dry_run: bool = False) -> tuple[int, int]:
    """Keep the lowest-price row per truckstop ID and normalized address.

    PostgreSQL ranks the rows, copies an available geocode to the winner, and
    deletes the others. The write path locks the table so the count and cleanup
    describe one consistent set of rows.
    """
    table = connection.ops.quote_name(FuelStation._meta.db_table)
    identity = (
        'lower(btrim(opis_truckstop_id)), lower(btrim(address)), '
        'lower(btrim(city)), upper(btrim(state))'
    )
    order = 'retail_price, source_file, source_row_number, id'
    with connection.cursor() as cursor:
        if not dry_run:
            cursor.execute(f'LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE')
        cursor.execute(f'''
            SELECT count(*), coalesce(sum(row_count - 1), 0)
            FROM (
                SELECT count(*) AS row_count
                FROM {table}
                GROUP BY {identity}
                HAVING count(*) > 1
            ) AS duplicates
        ''')
        groups, removed = cursor.fetchone()
        if dry_run or not removed:
            return groups, removed

        cursor.execute(f'''
            WITH ranked AS (
                SELECT
                    first_value(id) OVER (
                        PARTITION BY {identity} ORDER BY {order}
                    ) AS keeper_id,
                    first_value(id) OVER (
                        PARTITION BY {identity}
                        ORDER BY (location IS NULL), {order}
                    ) AS donor_id
                FROM {table}
            ), pairs AS (
                SELECT DISTINCT keeper_id, donor_id
                FROM ranked WHERE keeper_id <> donor_id
            )
            UPDATE {table} AS keeper
            SET latitude = donor.latitude,
                longitude = donor.longitude,
                location = donor.location,
                display_name = donor.display_name,
                geocoding_status = donor.geocoding_status,
                geocoding_error = donor.geocoding_error,
                osm_attempted_at = donor.osm_attempted_at
            FROM pairs JOIN {table} AS donor ON donor.id = pairs.donor_id
            WHERE keeper.id = pairs.keeper_id
              AND keeper.location IS NULL
              AND donor.location IS NOT NULL
        ''')
        cursor.execute(f'''
            WITH ranked AS (
                SELECT id, row_number() OVER (
                    PARTITION BY {identity} ORDER BY {order}
                ) AS position
                FROM {table}
            )
            DELETE FROM {table} AS station
            USING ranked
            WHERE station.id = ranked.id AND ranked.position > 1
        ''')
        if cursor.rowcount != removed:
            raise RuntimeError('Station cleanup count changed during the transaction')
    return groups, removed


@transaction.atomic
def _save_source_batch(batch: list[FuelStation]) -> None:
    source_file = batch[0].source_file
    existing = {
        station.source_row_number: station
        for station in FuelStation.objects.filter(
            source_file=source_file,
            source_row_number__in=[station.source_row_number for station in batch],
        )
    }
    to_create = []
    to_update = []
    for incoming in batch:
        station = existing.get(incoming.source_row_number)
        if station is None:
            to_create.append(incoming)
            continue
        address_changed = any(
            getattr(station, field) != getattr(incoming, field)
            for field in ('address', 'city', 'state')
        )
        if any(getattr(station, field) != getattr(incoming, field) for field in SOURCE_FIELDS):
            for field in SOURCE_FIELDS:
                setattr(station, field, getattr(incoming, field))
            if address_changed:
                station.latitude = None
                station.longitude = None
                station.location = None
                station.display_name = ''
                station.geocoding_status = FuelStation.GeocodingStatus.PENDING
                station.geocoding_error = ''
                station.osm_attempted_at = None
            to_update.append(station)
    if to_create:
        FuelStation.objects.bulk_create(to_create, batch_size=len(batch))
    if to_update:
        FuelStation.objects.bulk_update(
            to_update, fields=SOURCE_FIELDS + GEOCODING_FIELDS, batch_size=len(batch)
        )


def geocode_stations(
    source_file: str,
    client: LocationIQClient,
    *,
    batch_size: int,
    retry_no_match: bool = False,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """Save each processed batch; reruns skip rows already geocoded."""
    cache: dict[tuple[str, str, str, str], GeocodingResult | None] = {}
    for station in FuelStation.objects.filter(
        source_file=source_file, geocoding_status=FuelStation.GeocodingStatus.GEOCODED
    ).iterator(chunk_size=batch_size):
        cache[_cache_key(station)] = GeocodingResult(
            station.latitude, station.longitude, station.display_name
        )

    pending = stations_needing_geocoding(source_file, retry_no_match=retry_no_match)
    iterator = pending.iterator(chunk_size=batch_size)
    geocoded = no_match = processed = 0
    while True:
        remaining = batch_size if limit is None else min(batch_size, limit - processed)
        if remaining <= 0:
            break
        batch = list(islice(iterator, remaining))
        if not batch:
            break
        completed = []
        for station in batch:
            try:
                key = _cache_key(station)
                if key not in cache:
                    query, country_code = _geocoding_query(station)
                    result = client.forward_geocode(
                        query,
                        country_code=country_code,
                        expected_city=station.city,
                        expected_state=station.state,
                    )
                    if result is None:
                        country_name = 'Canada' if country_code == 'ca' else 'United States'
                        name_query = (
                            f'{station.name}, {station.city}, {station.state}, {country_name}'
                        )
                        result = client.forward_geocode(
                            name_query,
                            country_code=country_code,
                            expected_city=station.city,
                            expected_state=station.state,
                        )
                    cache[key] = result
                result = cache[key]
            except LocationIQError as exc:
                station.geocoding_status = FuelStation.GeocodingStatus.ERROR
                station.geocoding_error = str(exc)
                completed.append(station)
                _save_geocoding_batch(completed)
                raise LocationIQError(
                    f'Geocoding stopped at CSV row {station.source_row_number}: {exc}'
                ) from None

            if result is None:
                station.geocoding_status = FuelStation.GeocodingStatus.NO_MATCH
                station.geocoding_error = ''
                no_match += 1
            else:
                station.latitude = result.latitude.quantize(Decimal('0.00000001'))
                station.longitude = result.longitude.quantize(Decimal('0.00000001'))
                station.location = Point(float(station.longitude), float(station.latitude), srid=4326)
                station.display_name = result.display_name
                station.geocoding_status = FuelStation.GeocodingStatus.GEOCODED
                station.geocoding_error = ''
                geocoded += 1
            completed.append(station)
            processed += 1

        _save_geocoding_batch(completed)
        if progress:
            progress(geocoded, no_match)
    return geocoded, no_match


def _save_geocoding_batch(batch: list[FuelStation]) -> None:
    FuelStation.objects.bulk_update(batch, fields=GEOCODING_FIELDS, batch_size=len(batch))


def _geocoding_query(station: FuelStation) -> tuple[str, str]:
    country_code = 'ca' if station.state.upper() in CANADIAN_PROVINCES else 'us'
    country_name = 'Canada' if country_code == 'ca' else 'United States'
    return f'{station.address}, {station.city}, {station.state}, {country_name}', country_code


def _cache_key(station: FuelStation) -> tuple[str, str, str, str]:
    return station.name, station.address, station.city, station.state


def retry_unmatched_stations(
    client, *, batch_size: int, limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """Try Nominatim once per unresolved row, saving progress after each batch."""
    rows = FuelStation.objects.filter(
        geocoding_status__in=[FuelStation.GeocodingStatus.NO_MATCH, FuelStation.GeocodingStatus.ERROR],
        location__isnull=True,
        osm_attempted_at__isnull=True,
    ).order_by('pk')
    if limit is not None:
        rows = rows[:limit]
    found = missing = 0
    iterator = rows.iterator(chunk_size=batch_size)
    while batch := list(islice(iterator, batch_size)):
        completed = []
        for station in batch:
            query, country_code = _geocoding_query(station)
            country_name = 'Canada' if country_code == 'ca' else 'United States'
            queries = [query, f'{station.name}, {station.city}, {station.state}, {country_name}']
            try:
                result = None
                for search_query in queries:
                    result = _cached_nominatim_search(
                        client, search_query, country_code, station.city, station.state
                    )
                    if result is not None:
                        break
            except NominatimError as exc:
                station.geocoding_status = FuelStation.GeocodingStatus.ERROR
                station.geocoding_error = str(exc)
                completed.append(station)
                _save_geocoding_batch(completed)
                raise NominatimError(
                    f'OpenStreetMap geocoding stopped at station {station.pk}: {exc}'
                ) from None

            if result is None:
                station.geocoding_status = FuelStation.GeocodingStatus.NO_MATCH
                missing += 1
            else:
                station.latitude = result.latitude.quantize(Decimal('0.00000001'))
                station.longitude = result.longitude.quantize(Decimal('0.00000001'))
                station.location = Point(float(station.longitude), float(station.latitude), srid=4326)
                station.display_name = result.display_name
                station.geocoding_status = FuelStation.GeocodingStatus.GEOCODED
                found += 1
            station.geocoding_error = ''
            station.osm_attempted_at = timezone.now()
            completed.append(station)
        _save_geocoding_batch(completed)
        if progress:
            progress(found, missing)
    return found, missing


def _cached_nominatim_search(client, query, country_code, city, state):
    key = hashlib.sha256(json.dumps(
        [query, country_code, city, state], ensure_ascii=False
    ).encode('utf-8')).hexdigest()
    cached = NominatimSearchCache.objects.filter(key=key).first()
    if cached is not None:
        return (GeocodingResult(cached.latitude, cached.longitude, cached.display_name)
                if cached.found else None)
    result = client.forward_geocode(
        query, country_code=country_code, expected_city=city, expected_state=state
    )
    NominatimSearchCache.objects.create(
        key=key, query=query, found=result is not None,
        latitude=result.latitude if result else None,
        longitude=result.longitude if result else None,
        display_name=result.display_name if result else '',
    )
    return result
