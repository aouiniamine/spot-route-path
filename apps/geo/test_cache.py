"""Route response cache behavior without live provider calls."""

import json
import uuid
from datetime import timedelta
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import RouteCache
from .clients.routing import ProviderError


START = {'lat': 41.8781, 'lng': -87.6298}
FINISH = {'lat': 39.7392, 'lng': -104.9903}


def response(value):
    return Mock(to_dict=Mock(return_value={
        'route': {
            'id': value,
            'geometry': {'type': 'LineString', 'coordinates': [[-87.63, 41.88], [-104.99, 39.74]]},
            'distance_miles': 1000,
        },
        'fuel_stops': [],
        'fuel': {'total_fuel_cost_usd': '0.00'},
        'route_url': None,
    }))


class RouteCacheTests(TestCase):
    def post(self, body):
        return self.client.post(reverse('fuel-plan'), data=body, content_type='application/json')

    @patch('apps.geo.views.plan')
    @patch('apps.geo.views.RoutingClient')
    def test_same_coordinates_return_saved_json_without_provider(self, routing_client, planner):
        planner.return_value = response('first')
        first = self.post(json.dumps({'start': START, 'finish': FINISH}))
        # Different field order and whitespace should identify the same request.
        second = self.post(json.dumps({
            'finish': {'lng': FINISH['lng'], 'lat': FINISH['lat']},
            'start': {'lng': START['lng'], 'lat': START['lat']},
        }, indent=2))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(
            first.json()['route_url'],
            f'http://testserver/api/geo/route/{RouteCache.objects.get().id}',
        )
        self.assertEqual(RouteCache.objects.count(), 1)
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(routing_client.call_count, 1)
        cached = RouteCache.objects.get()
        self.assertIsNone(cached.response['route_url'])
        self.assertIsInstance(cached.id, uuid.UUID)
        self.assertEqual(len(cached.key), 64)
        self.assertGreater(cached.expires_at, cached.created_at + timedelta(hours=23))
        self.assertLess(cached.expires_at, cached.created_at + timedelta(hours=25))

    @patch('apps.geo.views.plan')
    @patch('apps.geo.views.RoutingClient')
    def test_expired_result_is_replaced(self, routing_client, planner):
        planner.side_effect = [response('first'), response('refreshed')]
        self.post(json.dumps({'start': START, 'finish': FINISH}))
        RouteCache.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        refreshed = self.post(json.dumps({'start': START, 'finish': FINISH}))

        self.assertEqual(refreshed.json()['route']['id'], 'refreshed')
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(RouteCache.objects.count(), 1)
        self.assertGreater(RouteCache.objects.get().expires_at, timezone.now())

    @patch('apps.geo.views.plan')
    @patch('apps.geo.views.RoutingClient')
    def test_new_result_purges_other_expired_rows(self, routing_client, planner):
        RouteCache.objects.create(
            key='x' * 64, request={}, response={'old': True},
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        planner.return_value = response('new')

        self.post(json.dumps({'start': START, 'finish': FINISH}))

        self.assertEqual(RouteCache.objects.count(), 1)
        self.assertEqual(RouteCache.objects.get().response['route']['id'], 'new')

    @patch('apps.geo.views.plan')
    @patch('apps.geo.views.RoutingClient')
    def test_route_direction_has_a_different_key(self, routing_client, planner):
        planner.side_effect = [response('outbound'), response('return')]
        outbound = self.post(json.dumps({'start': START, 'finish': FINISH}))
        returning = self.post(json.dumps({'start': FINISH, 'finish': START}))

        self.assertNotEqual(outbound.json(), returning.json())
        self.assertEqual(RouteCache.objects.count(), 2)
        self.assertEqual(planner.call_count, 2)

    @patch('apps.geo.views.plan')
    @patch('apps.geo.views.RoutingClient')
    def test_failed_plan_is_not_cached(self, routing_client, planner):
        planner.side_effect = [ProviderError('temporarily unavailable'), response('ok')]
        body = json.dumps({'start': START, 'finish': FINISH})
        failed = self.post(body)
        self.assertEqual(failed.status_code, 503)
        self.assertFalse(RouteCache.objects.exists())

        succeeded = self.post(body)
        self.assertEqual(succeeded.status_code, 200)
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(RouteCache.objects.count(), 1)

    @patch('apps.geo.views.plan')
    @patch('apps.geo.views.RoutingClient')
    def test_invalid_body_never_reaches_cache_or_provider(self, routing_client, planner):
        invalid = self.post('{')
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(RouteCache.objects.exists())
        planner.assert_not_called()
        routing_client.assert_not_called()

    @patch('apps.geo.views.plan')
    @patch('apps.geo.views.RoutingClient')
    def test_preview_renders_saved_geometry_without_routing_call(self, routing_client, planner):
        planner.return_value = response('saved')
        planned = self.post(json.dumps({'start': START, 'finish': FINISH}))
        url = urlsplit(planned.json()['route_url']).path

        preview = self.client.get(url)

        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview['Content-Type'].startswith('text/html'))
        self.assertNotIn('X-Frame-Options', preview)
        self.assertContains(preview, 'tiles.locationiq.com/v3/streets/vector.json')
        self.assertContains(preview, '[-87.63, 41.88]')
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(routing_client.call_count, 1)

    def test_unknown_or_expired_preview_returns_404(self):
        self.assertEqual(self.client.get(reverse('geo-route-preview', kwargs={'route_id': uuid.uuid4()})).status_code, 404)
        entry = RouteCache.objects.create(
            key='z' * 64, request={}, response=response('old').to_dict(),
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(self.client.get(reverse('geo-route-preview', kwargs={'route_id': entry.id})).status_code, 404)

    def test_preview_only_accepts_get(self):
        entry = RouteCache.objects.create(
            key='y' * 64, request={}, response=response('saved').to_dict(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.assertEqual(self.client.post(reverse('geo-route-preview', kwargs={'route_id': entry.id})).status_code, 405)
