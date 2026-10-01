"""Bounded LocationIQ routing calls. Never include the API key in errors."""

import time
import hashlib
import json
import math

import requests
from django.core.cache import cache


class ProviderError(Exception):
    pass


class NoRouteError(Exception):
    pass


class RoutingClient:
    BASE_URL = 'https://us1.locationiq.com/v1'
    # Approximate center of the contiguous US, near Lebanon, Kansas (USGS).
    US_CENTER_LAT = 39 + 50 / 60
    US_CENTER_LNG = -(98 + 35 / 60)
    US_RADIUS_MILES = 1700

    def __init__(self, key, session=None, max_calls=6, timeout=12):
        if not key:
            raise ProviderError('Routing provider is not configured')
        self.key = key
        self.session = session or requests.Session()
        self.max_calls = max_calls
        self.timeout = timeout
        self.calls = 0

    def _get(self, url, params):
        cache_input = json.dumps([url, params], sort_keys=True, separators=(',', ':'))
        cache_key = 'locationiq-route:' + hashlib.sha256(cache_input.encode()).hexdigest()
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        params = {'key': self.key, **params}
        for attempt in range(2):
            if self.calls >= self.max_calls:
                raise ProviderError('Routing request budget exceeded')
            self.calls += 1
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException:
                if attempt:
                    raise ProviderError('Routing provider could not be reached') from None
                continue
            if response.status_code in (429, 500, 502, 503, 504) and not attempt:
                if response.status_code == 429:
                    try:
                        delay = min(10, max(0, float(response.headers.get('Retry-After', '2'))))
                    except ValueError:
                        delay = 2
                else:
                    delay = 0.2
                time.sleep(delay)
                continue
            if response.status_code != 200:
                if response.status_code in (400, 404, 422):
                    raise NoRouteError('No drivable route was found')
                raise ProviderError(f'Routing provider returned HTTP {response.status_code}')
            try:
                payload = response.json()
                cache.set(cache_key, payload, timeout=900)
                return payload
            except ValueError:
                raise ProviderError('Routing provider returned invalid JSON') from None
        raise ProviderError('Routing provider failed')

    def is_us(self, point):
        """Cheap contiguous-US radius estimate; no provider request is made."""
        lat1 = math.radians(self.US_CENTER_LAT)
        lat2 = math.radians(point.lat)
        delta_lat = lat2 - lat1
        delta_lng = math.radians(point.lng - self.US_CENTER_LNG)
        haversine = (math.sin(delta_lat / 2) ** 2
                     + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2)
        miles = 3958.7613 * 2 * math.asin(min(1, math.sqrt(haversine)))
        return miles <= self.US_RADIUS_MILES

    def _url(self, service, points):
        if len(points) > 25:
            raise ProviderError('Too many routing points')
        joined = ';'.join(f'{point.lng:.8f},{point.lat:.8f}' for point in points)
        return f'{self.BASE_URL}/{service}/driving/{joined}'

    def directions(self, points):
        payload = self._get(self._url('directions', points), {
            'geometries': 'geojson', 'overview': 'full', 'steps': 'false',
        })
        try:
            if payload['code'] != 'Ok':
                raise NoRouteError('No drivable route was found')
            route = payload['routes'][0]
            geometry = route['geometry']
            legs = route['legs']
            if geometry['type'] != 'LineString' or len(geometry['coordinates']) < 2 or len(legs) != len(points) - 1:
                raise ValueError
            leg_meters = [float(leg['distance']) for leg in legs]
            if any(distance < 0 for distance in leg_meters):
                raise ValueError
            if abs(sum(leg_meters) - float(route['distance'])) > 10:
                raise ValueError
            return {'geometry': geometry, 'distance': float(route['distance']),
                    'duration': float(route['duration']), 'legs': leg_meters,
                    'waypoints': payload.get('waypoints', [])}
        except NoRouteError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError('Routing provider returned an invalid route') from None

    def matrix(self, points):
        payload = self._get(self._url('matrix', points), {'annotations': 'distance,duration'})
        try:
            if payload['code'] != 'Ok':
                raise NoRouteError('No drivable route was found')
            distances = payload['distances']
            if len(distances) != len(points) or any(len(row) != len(points) for row in distances):
                raise ValueError
            return distances
        except NoRouteError:
            raise
        except (KeyError, TypeError, ValueError):
            raise ProviderError('Routing provider returned an invalid distance matrix') from None
