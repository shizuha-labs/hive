"""Internal, ingress-blocked Hive capability-evidence HTTP endpoint."""
import hmac

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from fleet.capability_evidence import CapabilityEvidenceError, produce_evidence


def _authenticated(request) -> bool:
    expected = str(
        getattr(settings, 'HIVE_CAPABILITY_EVIDENCE_AUTH_TOKEN', '') or ''
    ).strip()
    supplied = str(request.META.get('HTTP_X_HIVE_INTERNAL_TOKEN', '') or '').strip()
    authorization = str(request.META.get('HTTP_AUTHORIZATION', '') or '')
    if authorization.startswith('Bearer '):
        supplied = authorization[7:].strip()
    return bool(expected and supplied) and hmac.compare_digest(supplied, expected)


class CapabilityEvidenceView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        if not _authenticated(request):
            return Response(
                {'error': 'internal_service_authentication_required'},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            evidence, _claims = produce_evidence(request.body)
        except CapabilityEvidenceError as exc:
            return Response(exc.as_dict(), status=exc.http_status)
        return Response({'evidence': evidence}, status=status.HTTP_200_OK)
