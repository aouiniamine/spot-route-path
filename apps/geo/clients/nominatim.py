"""Forward geocoding through the OpenStreetMap Nominatim Search API."""

import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import requests

from .locationiq import GeocodingResult


class NominatimError(Exception):
    """Nominatim was unavailable or rejected a request."""


class NominatimClient:
    PUBLIC_URL = 'https://nominatim.openstreetmap.org/search'

    def __init__(self, contact_email: str, *, base_url: str = PUBLIC_URL,
                 session: requests.Session | None = None, timeout: float = 15.0):
        parsed = urlparse(base_url)
        if parsed.scheme != 'https' or not parsed.netloc:
            raise ValueError('Nominatim URL must use HTTPS')
        self.is_public = parsed.hostname == 'nominatim.openstreetmap.org'
        if self.is_public and (not contact_email or '@' not in contact_email):
            raise ValueError('NOMINATIM_CONTACT_EMAIL is required for public Nominatim')
        if timeout <= 0:
            raise ValueError('Nominatim timeout must be greater than zero')
        self.base_url = base_url
        self.contact_email = contact_email
        self.session = session or requests.Session()
        self.timeout = timeout

    def forward_geocode(self, address: str, *, country_code: str | None = None,
                        expected_city: str | None = None, expected_state: str | None = None):
        if not address.strip():
            raise ValueError('An address is required for forward geocoding')
        params = {'q': address.strip(), 'format': 'jsonv2', 'addressdetails': 1, 'limit': 10}
        if country_code:
            params['countrycodes'] = country_code
        if self.contact_email:
            params['email'] = self.contact_email
        headers = {'User-Agent': f'SpotterFuelStationGeocoder/1.0 ({self.contact_email or "self-hosted"})'}

        try:
            response = self.session.get(
                self.base_url, params=params, headers=headers, timeout=self.timeout
            )
        except requests.RequestException:
            raise NominatimError('Nominatim could not be reached') from None
        if response.status_code != 200:
            raise NominatimError(f'Nominatim rejected the request (HTTP {response.status_code})')
        try:
            candidates = response.json()
            if not isinstance(candidates, list):
                raise ValueError('Expected a list')
            for candidate in candidates:
                if not _matches(candidate, country_code, expected_city, expected_state):
                    continue
                latitude = Decimal(str(candidate['lat']))
                longitude = Decimal(str(candidate['lon']))
                display_name = candidate['display_name']
                if (not latitude.is_finite() or not longitude.is_finite()
                        or not -90 <= latitude <= 90 or not -180 <= longitude <= 180
                        or not isinstance(display_name, str) or not display_name.strip()):
                    raise ValueError('Invalid coordinates or display name')
                return GeocodingResult(latitude, longitude, display_name)
            return None
        except (ValueError, TypeError, KeyError, InvalidOperation):
            raise NominatimError('Nominatim returned an invalid search result') from None


def _normalize(value):
    if not isinstance(value, str):
        return ''
    ascii_value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    return ''.join(character for character in ascii_value.casefold() if character.isalnum())


def _matches(candidate, country_code, city, state):
    if not isinstance(candidate, dict) or not isinstance(candidate.get('address'), dict):
        return False
    address = candidate['address']
    if country_code and _normalize(address.get('country_code')) != _normalize(country_code):
        return False
    if city:
        cities = (address.get(field) for field in (
            'city', 'town', 'village', 'municipality', 'hamlet'
        ))
        if not any(_normalize(value) == _normalize(city) for value in cities):
            return False
    if state:
        subdivision = address.get('ISO3166-2-lvl4', '')
        if not isinstance(subdivision, str):
            return False
        if _normalize(subdivision.rsplit('-', 1)[-1]) != _normalize(state):
            return False
    return True
