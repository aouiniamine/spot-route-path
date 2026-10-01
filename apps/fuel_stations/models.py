from django.contrib.gis.db import models


class FuelStation(models.Model):
    class GeocodingStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        GEOCODED = 'geocoded', 'Geocoded'
        NO_MATCH = 'no_match', 'No match'
        ERROR = 'error', 'Error'

    source_file = models.CharField(max_length=512)
    source_row_number = models.PositiveIntegerField()
    opis_truckstop_id = models.CharField(max_length=32, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=512)
    city = models.CharField(max_length=255)
    state = models.CharField(max_length=16)
    rack_id = models.CharField(max_length=32)
    retail_price = models.DecimalField(max_digits=12, decimal_places=8)

    latitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    longitude = models.DecimalField(max_digits=12, decimal_places=8, null=True, blank=True)
    location = models.PointField(geography=True, srid=4326, null=True, blank=True)
    display_name = models.TextField(blank=True)
    geocoding_status = models.CharField(
        max_length=16, choices=GeocodingStatus, default=GeocodingStatus.PENDING
    )
    geocoding_error = models.TextField(blank=True)
    osm_attempted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['source_file', 'source_row_number'],
                name='unique_fuel_station_source_row',
            )
        ]
        ordering = ['source_file', 'source_row_number']

    def __str__(self):
        return f'{self.name} ({self.opis_truckstop_id})'
