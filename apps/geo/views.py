from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .dtos import RequestValidationError, FuelRouteRequest
from .planner import PlanningUnavailable, plan
from .clients.routing import NoRouteError, ProviderError, RoutingClient


@require_POST
def fuel_plan(request):
    try:
        req_body = FuelRouteRequest.from_json(request.body)
    except RequestValidationError as error:
        return JsonResponse({'error': {'code': 'invalid_request', 'message': str(error)}}, status=400)
    try:
        result = plan(req_body, RoutingClient(settings.LOCATIONIQ_API_KEY))
        return JsonResponse(result.to_dict())
    except ValueError as error:
        return JsonResponse({'error': {'code': 'invalid_location', 'message': str(error)}}, status=422)
    except NoRouteError:
        return JsonResponse({'error': {'code': 'no_route', 'message': 'No drivable route was found'}}, status=422)
    except (ProviderError, PlanningUnavailable) as error:
        return JsonResponse({'error': {'code': 'planning_unavailable', 'message': str(error)}}, status=503)
