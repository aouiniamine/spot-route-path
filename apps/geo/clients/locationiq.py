"""Small adapter for LocationIQ's Search / Forward Geocoding API."""

import time
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests


class LocationIQError(Exception):
    """A request failed or LocationIQ returned an unusable response."""


@dataclass(frozen=True)
class GeocodingResult:
    latitude: Decimal
    longitude: Decimal
    display_name: str


class LocationIQClient:
    SEARCH_URL = 'https://api.locationiq.com/v1/search'

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        request_interval: float = 1.0,
        timeout: float = 15.0,
        max_retries: int = 3,
    ):
        if not api_key:
            raise ValueError('A LocationIQ API key is required')
        if request_interval < 0 or timeout <= 0 or max_retries < 0:
            raise ValueError('Invalid LocationIQ request timing or retry setting')
        self.api_key = api_key
        self.session = session or requests.Session()
        self.request_interval = request_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_request_at: float | None = None

    def forward_geocode(
        self,
        address: str,
        *,
        country_code: str | None = None,
        expected_city: str | None = None,
        expected_state: str | None = None,
    ):
        """Return a candidate in the expected city and state, or None."""
        query = address.strip()
        if not query:
            raise ValueError('An address is required for forward geocoding')

        params = {
            'key': self.api_key,
            'q': query,
            'format': 'json',
            'limit': 10,
            'addressdetails': 1,
            'normalizecity': 1,
            'statecode': 1,
        }
        if country_code:
            params['countrycodes'] = country_code

        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            try:
                response = self.session.get(self.SEARCH_URL, params=params, timeout=self.timeout)
            except requests.RequestException:
                if attempt == self.max_retries:
                    # A requests exception may contain the URL and API key.
                    raise LocationIQError('LocationIQ could not be reached') from None
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 404:
                return None
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == self.max_retries:
                    raise LocationIQError(
                        f'LocationIQ failed after retries (HTTP {response.status_code})'
                    )
                retry_after = response.headers.get('Retry-After', '')
                try:
                    delay = max(0.0, min(float(retry_after), 60.0))
                except ValueError:
                    delay = 2 ** attempt
                time.sleep(delay)
                continue
            if response.status_code != 200:
                raise LocationIQError(f'LocationIQ rejected the request (HTTP {response.status_code})')

            try:
                results = response.json()
                if not isinstance(results, list):
                    raise ValueError('Expected a list of search results')
                for candidate in results:
                    if not self._matches_place(candidate, expected_city, expected_state):
                        continue
                    latitude = Decimal(str(candidate['lat']))
                    longitude = Decimal(str(candidate['lon']))
                    display_name = candidate['display_name']
                    if (
                        not latitude.is_finite()
                        or not longitude.is_finite()
                        or not -90 <= latitude <= 90
                        or not -180 <= longitude <= 180
                        or not isinstance(display_name, str)
                        or not display_name.strip()
                    ):
                        raise ValueError('Invalid geocoding coordinates or display name')
                    return GeocodingResult(latitude, longitude, display_name)
            except (ValueError, TypeError, KeyError, InvalidOperation, IndexError):
                raise LocationIQError('LocationIQ returned an invalid search result') from None
            return None

        raise AssertionError('Unreachable LocationIQ retry state')

    def _wait_for_rate_limit(self):
        if self._last_request_at is not None:
            remaining = self.request_interval - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _matches_place(candidate, expected_city, expected_state):
        if expected_city is None and expected_state is None:
            return True
        if not isinstance(candidate, dict) or not isinstance(candidate.get('address'), dict):
            return False
        address = candidate['address']
        if expected_city and _normalize_place(address.get('city')) != _normalize_place(expected_city):
            return False
        if expected_state and _normalize_place(address.get('state_code')) != _normalize_place(expected_state):
            return False
        return True


def _normalize_place(value):
    if not isinstance(value, str):
        return ''
    ascii_value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    return ''.join(character for character in ascii_value.casefold() if character.isalnum())
