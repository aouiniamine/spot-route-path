"""Database-backed, one-day cache for successful route responses."""

from dataclasses import asdict
from datetime import timedelta
import hashlib
import json

from django.utils import timezone

from .models import RouteCache


ROUTE_CACHE_TTL = timedelta(days=1)


def _request_identity(request):
    payload = asdict(request)
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest(), payload


def get_cached_route(request):
    key, _ = _request_identity(request)
    return (RouteCache.objects.filter(key=key, expires_at__gt=timezone.now())
            .first())


def cache_route(request, response):
    key, payload = _request_identity(request)
    now = timezone.now()
    RouteCache.objects.filter(expires_at__lte=now).delete()
    entry, _ = RouteCache.objects.update_or_create(
        key=key,
        defaults={
            'request': payload,
            'response': response,
            'expires_at': now + ROUTE_CACHE_TTL,
        },
    )
    return entry


def invalidate_route_cache():
    """Discard saved route prices after station data changes."""
    RouteCache.objects.all().delete()
