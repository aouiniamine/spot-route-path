from .models import FuelStation


def stations_by_opis_id(opis_truckstop_id: str):
    """A truckstop ID can occur on multiple CSV rows with different prices."""
    return FuelStation.objects.filter(opis_truckstop_id=opis_truckstop_id)


def stations_needing_geocoding(source_file: str, *, retry_no_match: bool = False):
    statuses = [FuelStation.GeocodingStatus.PENDING, FuelStation.GeocodingStatus.ERROR]
    if retry_no_match:
        statuses.append(FuelStation.GeocodingStatus.NO_MATCH)
    return FuelStation.objects.filter(
        source_file=source_file, geocoding_status__in=statuses
    ).order_by('source_row_number')
