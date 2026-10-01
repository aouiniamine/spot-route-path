from django.db import models

# Create your models here.
from django.db import models


class NominatimSearchCache(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    query = models.TextField()
    found = models.BooleanField()
    latitude = models.DecimalField(max_digits=11, decimal_places=8, null=True)
    longitude = models.DecimalField(max_digits=12, decimal_places=8, null=True)
    display_name = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
