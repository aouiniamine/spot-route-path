from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.clickjacking import xframe_options_exempt
from django.utils import timezone

from .dtos import RequestValidationError, FuelRouteRequest
from .cache import cache_route, get_cached_route
from .planner import PlanningUnavailable, plan
from .clients.routing import NoRouteError, ProviderError, RoutingClient
from .models import RouteCache


def _with_route_url(request, entry):
    response = dict(entry.response)
    response['route_url'] = request.build_absolute_uri(
        reverse('geo-route-preview', kwargs={'route_id': entry.id})
    )
    return response


@require_POST
def fuel_plan(request):
    try:
        req_body = FuelRouteRequest.from_json(request.body)
    except RequestValidationError as error:
        return JsonResponse({'error': {'code': 'invalid_request', 'message': str(error)}}, status=400)
    cached = get_cached_route(req_body)
    if cached is not None:
        return JsonResponse(_with_route_url(request, cached))
    try:
        result = plan(req_body, RoutingClient(settings.LOCATIONIQ_API_KEY))
        entry = cache_route(req_body, result.to_dict())
        return JsonResponse(_with_route_url(request, entry))
    except ValueError as error:
        return JsonResponse({'error': {'code': 'invalid_location', 'message': str(error)}}, status=422)
    except NoRouteError:
        return JsonResponse({'error': {'code': 'no_route', 'message': 'No drivable route was found'}}, status=422)
    except (ProviderError, PlanningUnavailable) as error:
        return JsonResponse({'error': {'code': 'planning_unavailable', 'message': str(error)}}, status=503)


@xframe_options_exempt
@require_GET
def route_preview(request, route_id):
    entry = get_object_or_404(RouteCache, id=route_id, expires_at__gt=timezone.now())
    return render(request, 'geo/route_preview.html', {
        'map_data': entry.response,
        'map_key': settings.LOCATIONIQ_MAPS_PUBLIC_KEY,
    })
