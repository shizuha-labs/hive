"""
Federated JWT Authentication for Hive.

Same pattern as other Shizuha services — validates JWT tokens
issued by Shizuha ID without requiring local user records.
"""
import base64
import json
import logging

import jwt
from django.conf import settings
from django.middleware.csrf import CsrfViewMiddleware
from rest_framework import authentication, exceptions
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from hive_platform.token_revocation import is_token_revoked

logger = logging.getLogger(__name__)


class FederatedUser:
    """Lightweight user object from JWT claims."""

    def __init__(self, user_id, email, username, is_staff=False, is_superuser=False,
                 organization_memberships=None, enabled_services=None, is_agent=False):
        self.id = user_id
        self.pk = user_id
        self.email = email
        self.username = username
        self.is_staff = is_staff
        self.is_superuser = is_superuser
        # AI agent / service account (Shizuha-ID `is_agent` claim, from profile
        # account_type=='agent'). Federated services use it to refuse human-only
        # flows for agents — e.g. an agent must not spawn its own personal Shizuha.
        self.is_agent = bool(is_agent)
        self.is_authenticated = True
        self.is_active = True
        self.organization_memberships = organization_memberships or {}
        # None means legacy/unrestricted; an explicit list is restrictive.
        self.enabled_services = enabled_services

    def __str__(self):
        return f'FederatedUser({self.username})'


def _normalise_user_id(value):
    """Return the integer Shizuha-ID user id used by Hive owner_id fields."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _token_shape_fingerprint(token_str):
    """Sanitized structural fingerprint of a bearer token for auth-failure logs
    (PLAT-8183).

    Never includes token bytes — only the segment count and, per segment, the
    length plus whether it base64url-decodes and JSON-parses. That is enough to
    pinpoint the malformation class (wrong segment count, non-ASCII bytes,
    padding/base64 failure, header JSON corruption) from logs alone while
    leaking nothing about the credential's value.
    """
    try:
        segments = token_str.split('.')
    except Exception:
        return 'unstringable'
    descriptions = []
    for segment in segments[:4]:
        info = f'len={len(segment)}'
        try:
            padded = segment + '=' * (-len(segment) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode('ascii'))
            info += f',b64=ok,bytes={len(decoded)}'
            try:
                json.loads(decoded.decode('utf-8'))
                info += ',json=ok'
            except Exception:
                info += ',json=no'
        except UnicodeEncodeError:
            info += ',b64=non-ascii'
        except Exception:
            info += ',b64=no'
        descriptions.append(info)
    more = f',+{len(segments) - 4} more' if len(segments) > 4 else ''
    return f'segments={len(segments)};[{ " | ".join(descriptions) }{more}]'


class FederatedJWTAuthentication(authentication.BaseAuthentication):
    """
    Authenticate using JWT tokens from Shizuha ID.
    No local user database needed. Browser UI routes accept the same token from
    the Shizuha-ID cookie so rendered forms can authenticate without injecting
    an Authorization header.
    """

    @classmethod
    def _jwks_client(cls):
        jwks_url = getattr(settings, 'SHIZUHA_JWKS_URL', '')
        if not jwks_url:
            platform_url = getattr(settings, 'SHIZUHA_PLATFORM_URL', '').rstrip('/')
            if platform_url:
                jwks_url = f'{platform_url}/shizuha-id/.well-known/jwks.json'
        if not jwks_url:
            raise exceptions.AuthenticationFailed('RS256 JWKS URL is not configured.')
        return jwt.PyJWKClient(jwks_url)

    @classmethod
    def _validate_rs256_token(cls, token_str):
        # Canonical contract (PLAT-986/PLAT-675): REQUIRE kid — never resolve a
        # kid-less RS256 token to an arbitrary key. PyJWKClient also enforces this,
        # but make the contract explicit and fail closed before any network call.
        try:
            header = jwt.get_unverified_header(token_str)
        except Exception as e:
            raise exceptions.AuthenticationFailed(f'Invalid token: {e}') from e
        if not header.get('kid'):
            raise exceptions.AuthenticationFailed('Invalid token: RS256 token missing kid')
        try:
            signing_key = cls._jwks_client().get_signing_key_from_jwt(token_str)
            payload = jwt.decode(
                token_str,
                signing_key.key,
                algorithms=['RS256'],
                options={'verify_aud': False, 'verify_iss': False},
            )
        except Exception as e:
            raise exceptions.AuthenticationFailed(f'Invalid token: {e}') from e
        if payload.get('token_type') != 'access':
            raise exceptions.AuthenticationFailed('Invalid token: Token is not an access token')
        return payload


    @classmethod
    def validate_token(cls, token_str):
        """Validate a Shizuha ID bearer token and return (token, claims)."""
        # CANONICAL JWKS verifier — keep in sync, do not fork (RS256 migration,
        # PLAT-986/PLAT-675). alg-allowlist {HS256, RS256} only — reject alg='none'
        # and any other alg up front; then route STRICTLY by the declared alg
        # (HS256 → shared SIGNING_KEY via simplejwt; RS256 → JWKS) so an HS256/RS256
        # key-confusion token can never be validated against the wrong key.
        try:
            header = jwt.get_unverified_header(token_str) or {}
        except Exception as e:
            # PLAT-8183: the bare "malformed header" detail made a fleet-wide
            # broker-token regression undiagnosable — neither the 403 nor the
            # logs said WHY the parse failed (segment count? base64? JSON?
            # encoding?). Log a sanitized structural fingerprint (lengths and
            # per-segment decode classes only — never token bytes) plus the
            # parser's own exception, and carry the exception class in the
            # detail so the next probe self-identifies the failure class.
            shape = _token_shape_fingerprint(token_str)
            logger.error(
                'JWT header parse failed: %s: %s; token shape: %s',
                type(e).__name__, e, shape,
            )
            raise exceptions.AuthenticationFailed(
                f'Invalid token: malformed header ({type(e).__name__})'
            ) from e
        alg = (header.get('alg') or '').upper()
        if alg not in ('HS256', 'RS256'):
            raise exceptions.AuthenticationFailed(f'Invalid token: unsupported alg {alg!r}')
        if alg == 'RS256':
            validated = cls._validate_rs256_token(token_str)
        else:
            try:
                validated = AccessToken(token_str)
            except (InvalidToken, TokenError) as e:
                raise exceptions.AuthenticationFailed(f'Invalid token: {e}') from e

        # PLAT-1023 (canonical contract, PLAT-986/PLAT-1032): enforce token
        # revocation on BOTH paths after they converge — a validly-signed but
        # REVOKED OAuth token must be rejected. The RS256 path returns a plain
        # payload dict; the HS256 AccessToken exposes `.payload`. The shared
        # client skips non-OAuth tokens (no client_id) and FAILS CLOSED on any
        # revocation-lookup error. Keep in sync with claws/token_revocation.py.
        claims = validated.payload if hasattr(validated, 'payload') else validated
        if is_token_revoked(claims, token_str):
            raise exceptions.AuthenticationFailed('Invalid token: token has been revoked')
        return validated, claims

    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        token_str = ''
        token_from_cookie = False
        if auth_header.startswith('Bearer '):
            token_str = auth_header[7:]
        else:
            cookie_name = getattr(settings, 'HIVE_BROWSER_JWT_COOKIE', 'shizuha-access-token')
            token_str = request.COOKIES.get(cookie_name) or request.COOKIES.get('access_token', '')
            token_from_cookie = bool(token_str)
        if not token_str:
            return None

        validated, claims = self.validate_token(token_str)

        if token_from_cookie:
            self.enforce_csrf(request)
        # Preserve the raw, already-validated owner token for provisioning. DRF
        # otherwise exposes only the parsed AccessToken object, while OpenClaw's
        # bootstrap secret needs the bearer value that the API path used to get
        # from the Authorization header.
        request._hive_bearer_token = token_str

        is_staff = bool(claims.get('is_staff', False))
        is_superuser = bool(claims.get('is_superuser', False))
        is_agent = bool(claims.get('is_agent', False))
        enabled_services = None if (is_staff or is_superuser) else claims.get('enabled_services')
        user = FederatedUser(
            user_id=_normalise_user_id(claims.get('user_id', 0)),
            email=claims.get('email', ''),
            username=claims.get('username', ''),
            is_staff=is_staff,
            is_superuser=is_superuser,
            is_agent=is_agent,
            organization_memberships=claims.get('organization_memberships', {}),
            enabled_services=enabled_services,
        )

        return (user, validated)

    def enforce_csrf(self, request):
        """Cookie JWT auth has ambient browser credentials; require CSRF."""
        check = CsrfViewMiddleware(lambda req: None)
        check.process_request(request)
        reason = check.process_view(request, None, (), {})
        if reason:
            raise exceptions.PermissionDenied(f'CSRF Failed: {reason}')
