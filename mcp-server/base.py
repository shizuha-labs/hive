"""
Base utilities for Shizuha MCP Servers.

Provides common functionality for all MCP servers including:
- JWT token handling
- API request utilities
- Standard error formatting

Configuration can be provided via:
1. Command-line arguments (highest priority) - for callers that don't pass env vars
2. Environment variables (fallback)

Command-line arguments:
  --jwt-token TOKEN   JWT token for authentication
  --api-url URL       Base URL for the service API

Environment variables:
  SHIZUHA_BASE_URL    Base URL for all services (e.g., https://shizuha.com)
                      Each server resolves to {SHIZUHA_BASE_URL}/{service_name}
  SHIZUHA_{SERVICE}_URL  Per-service override (e.g., SHIZUHA_PULSE_URL)
  JWT_TOKEN           JWT token for authentication
"""

import argparse
import json
import logging
import os
import sys
from typing import Any, Callable, Optional


def _mcp_tls_verify_setting() -> bool | str:
    """Return requests.verify for MCP server upstream calls.

    Default stays False for existing internal/self-signed Shizuha services. Set
    SHIZUHA_MCP_TLS_VERIFY=true/1/yes/on to enforce default CA verification, or
    set it to a CA bundle path for public/external endpoints.
    """
    raw = os.environ.get("SHIZUHA_MCP_TLS_VERIFY", "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return raw

import jwt
import requests

# Set up logging to stderr (stdout is used for MCP protocol)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)


def parse_mcp_args() -> argparse.Namespace:
    """
    Parse command-line arguments for MCP servers.

    This allows configuration via args (for callers that don't pass env vars)
    in addition to environment variables.
    """
    parser = argparse.ArgumentParser(description="Shizuha MCP Server")
    parser.add_argument(
        "--jwt-token",
        help="JWT token for authentication (overrides environment variable)",
    )
    parser.add_argument(
        "--api-url",
        help="Base URL for the service API (overrides environment variable)",
    )
    parser.add_argument(
        "--stateless-http",
        dest="stateless_http",
        action="store_true",
        default=False,
        help=(
            "Run FastMCP in stateless-HTTP mode (no server-side session state). "
            "Safe for multi-replica deployments without ClientIP sessionAffinity. "
            "Equivalent to SHIZUHA_MCP_STATELESS_HTTP=true."
        ),
    )
    # Parse known args only - ignore unknown args that might be passed by MCP clients
    args, _ = parser.parse_known_args()
    return args


class ShizuhaMCPBase:
    """Base class for Shizuha MCP servers."""

    def __init__(
        self,
        service_name: str,
        default_port: int,
        api_prefix: str = "/api",
        api_url: Optional[str] = None,
        jwt_token: Optional[str] = None,
        request_context_getter: Optional[Callable[[], Any]] = None,
    ):
        """
        Initialize the MCP base.

        Args:
            service_name: Name of the service (e.g., 'pulse', 'wiki')
            default_port: Default port for the service
            api_prefix: API path prefix (default: '/api')
            api_url: Override API URL (highest priority)
            jwt_token: Override JWT token (highest priority)
            request_context_getter: Optional callback that returns the active
                FastMCP request context for extracting per-request headers.
        """
        self.service_name = service_name
        self.logger = logging.getLogger(f"shizuha-{service_name}")
        self._request_context_getter = request_context_getter

        # Parse command-line arguments (for CLI argument compatibility)
        args = parse_mcp_args()

        # Configuration priority: constructor > CLI args > environment > defaults
        env_prefix = f"SHIZUHA_{service_name.upper()}"

        # API URL resolution chain:
        #   constructor > CLI arg > service-specific env > SHIZUHA_BASE_URL > generic env
        # SHIZUHA_BASE_URL builds the URL as {base}/{service_name} (matches nginx routing)
        base_url = os.environ.get("SHIZUHA_BASE_URL", "").rstrip("/")
        self.api_url = (
            api_url
            or args.api_url
            or os.environ.get(f"{env_prefix}_URL")
            or (f"{base_url}/{service_name}" if base_url else None)
            or os.environ.get("API_URL")
            or ""
        )
        if not self.api_url:
            raise ValueError(
                f"No API URL configured for {service_name}. "
                f"Set SHIZUHA_BASE_URL or SHIZUHA_{service_name.upper()}_URL."
            )

        # JWT Token: constructor > CLI arg > service-specific env > generic env > empty
        self.jwt_token = (
            jwt_token
            or args.jwt_token
            or os.environ.get(f"{env_prefix}_JWT_TOKEN")
            or os.environ.get("JWT_TOKEN")
            or ""
        )

        self.api_prefix = api_prefix

        # User delegation token (dual-token model for chatbot sessions).
        # When present, allows MCP tools to make API calls on behalf of the
        # chatting user (not the agent). Services enforce the user's permissions.
        self.user_jwt_token = os.environ.get(f"{env_prefix}_USER_JWT_TOKEN", "")

        # Log configuration source for debugging
        if jwt_token:
            self.logger.info(f"JWT token provided via constructor")
        elif args.jwt_token:
            self.logger.info(f"JWT token provided via CLI argument")
        elif os.environ.get(f"{env_prefix}_JWT_TOKEN"):
            self.logger.info(f"JWT token from {env_prefix}_JWT_TOKEN env var")
        else:
            self.logger.info(
                f"No static JWT token configured for {service_name}; "
                f"expecting per-request Authorization header"
            )

        if self.user_jwt_token:
            self.logger.info(f"User delegation token available")

    @staticmethod
    def _extract_bearer_token(raw_value: Optional[str]) -> str:
        """Extract a bearer token from header-style values."""
        if not raw_value:
            return ""

        value = str(raw_value).strip()
        if not value:
            return ""

        lower = value.lower()
        if lower.startswith("bearer "):
            return value[7:].strip()
        return value

    def _get_request_headers(self) -> dict[str, str]:
        """
        Read incoming MCP HTTP request headers for the current tool call.

        Falls back to empty dict when request context is unavailable
        (e.g., stdio/local invocation).
        """
        request = None
        try:
            if self._request_context_getter:
                context = self._request_context_getter()
            else:
                # FastMCP stores request context in a ContextVar at runtime.
                from mcp.server.lowlevel.server import request_ctx

                context = request_ctx.get()
            request = getattr(context, "request", None)
            if request is None:
                request_context = getattr(context, "request_context", None)
                request = getattr(request_context, "request", None)
        except Exception:
            return {}

        if request is None:
            return {}

        raw_headers = getattr(request, "headers", None)
        if raw_headers is None:
            return {}

        try:
            header_items = raw_headers.items()
        except Exception:
            return {}

        normalized_headers = {}
        for key, value in header_items:
            if value is None:
                continue
            normalized_headers[str(key).lower()] = str(value)
        return normalized_headers

    def _get_effective_agent_token(self) -> str:
        """Use caller headers; static fallback is only for non-enforced transports."""
        headers = self._get_request_headers()
        for header_name in ("authorization", "x-shizuha-agent-authorization"):
            token = self._extract_bearer_token(headers.get(header_name))
            if token:
                return token
        return "" if _enforce_oauth() else self.jwt_token

    def _get_effective_user_token(self) -> str:
        """Use caller delegation; static fallback is only for non-enforced transports."""
        headers = self._get_request_headers()
        for header_name in (
            "x-shizuha-user-authorization",
            "x-shizuha-user-jwt-token",
            "x-shizuha-user-jwt",
        ):
            token = self._extract_bearer_token(headers.get(header_name))
            if token:
                return token
        return "" if _enforce_oauth() else self.user_jwt_token

    def get_headers(
        self,
        as_user: bool = False,
        organization_id: Optional[int] = None,
    ) -> dict:
        """
        Get headers for API requests.

        Args:
            as_user: If True, use the user delegation token instead of the
                     agent token. This makes the API call on behalf of the
                     chatting user, so the backend service enforces the user's
                     permissions (not the agent's).
            organization_id: Optional organization context. When provided, sets
                     `X-Organization-ID`. When None, no header is sent and the
                     downstream service decides whether to aggregate across the
                     caller's org memberships (reads) or 400 (writes).

                     Falls back to env var `SHIZUHA_ORGANIZATION_ID` (set by
                     the agent runtime to pin a default org) if no explicit
                     value is given. Never auto-derives from JWT — agents must
                     pick consciously.
        """
        user_token = self._get_effective_user_token()
        agent_token = self._get_effective_agent_token()

        if as_user:
            if not user_token:
                raise ValueError(
                    f"User delegation token not available for {self.service_name}. "
                    f"The user has not shared access with this agent."
                )
            headers = {
                "Authorization": f"Bearer {user_token}",
                "Content-Type": "application/json",
            }
        else:
            if not agent_token:
                raise ValueError(f"JWT token not configured for {self.service_name}")
            headers = {
                "Authorization": f"Bearer {agent_token}",
                "Content-Type": "application/json",
            }

        # Org resolution: explicit param > env override > unset.
        # Stays unset by default so reads aggregate across the caller's orgs.
        resolved_org = organization_id
        if resolved_org is None:
            env_org = os.environ.get("SHIZUHA_ORGANIZATION_ID", "").strip()
            if env_org:
                try:
                    resolved_org = int(env_org)
                except ValueError:
                    resolved_org = None
        if resolved_org is not None:
            headers["X-Organization-ID"] = str(resolved_org)
        return headers

    @property
    def has_user_context(self) -> bool:
        """Whether a user delegation token is available."""
        return bool(self._get_effective_user_token())

    def api_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
        timeout: int = 30,
        authenticated: bool = True,
        as_user: bool = False,
        organization_id: Optional[int] = None,
    ) -> dict:
        """
        Make an API request to the service.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE)
            endpoint: API endpoint path
            params: Query parameters
            data: JSON body data
            files: Files for multipart upload
            timeout: Request timeout in seconds
            authenticated: If True, include JWT token in headers. Set to False for public endpoints like login/register.
            as_user: If True, use user delegation token (acts on behalf of
                     the chatting user, not the agent).
            organization_id: Optional org scope. When provided, sent as
                     `X-Organization-ID` so the service narrows results to
                     that org. When None, no header is sent — read endpoints
                     aggregate across the caller's org memberships, write
                     endpoints reject with a 400 listing valid orgs.

        Returns:
            Response JSON or error dict
        """
        url = f"{self.api_url}{self.api_prefix}/{endpoint.lstrip('/')}"
        self.logger.info(
            f"API {method} {url} (as_user={as_user}, org={organization_id})"
        )

        try:
            if authenticated:
                headers = self.get_headers(as_user=as_user, organization_id=organization_id)
            else:
                # Public endpoint - no auth needed
                headers = {"Content-Type": "application/json"}

            if files:
                # Remove Content-Type for multipart
                del headers["Content-Type"]

            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=data if not files else None,
                data=data if files else None,
                files=files,
                timeout=timeout,
                verify=_mcp_tls_verify_setting(),
            )
            response.raise_for_status()

            # Handle empty responses
            if response.status_code == 204 or not response.content:
                return {"success": True}

            return response.json()
        except requests.exceptions.HTTPError as e:
            error_detail = ""
            try:
                error_detail = e.response.json()
            except Exception:
                error_detail = e.response.text
            self.logger.error(f"API error: {e} - {error_detail}")
            return {"error": str(e), "detail": error_detail, "status_code": e.response.status_code}
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Request error: {e}")
            return {"error": str(e)}

    def get_identity(self) -> dict:
        """
        Get the agent's own identity from JWT token.

        Returns the full `organization_memberships` dict so the agent can
        see every org it belongs to (with its role per org). Use this to
        pick the right `organization_id` when calling write endpoints.
        """
        token = self._get_effective_agent_token()
        if not token:
            return {"error": "No JWT token configured"}

        try:
            payload = jwt.decode(token, options={"verify_signature": False})
            raw_memberships = payload.get("organization_memberships") or {}
            normalized_memberships = {}
            if isinstance(raw_memberships, dict):
                for k, v in raw_memberships.items():
                    try:
                        normalized_memberships[int(k)] = str(v)
                    except (TypeError, ValueError):
                        continue
            return {
                "user_id": payload.get("user_id"),
                "email": payload.get("email"),
                "username": payload.get("username"),
                "first_name": payload.get("first_name"),
                "last_name": payload.get("last_name"),
                "is_agent": payload.get("is_agent", False),
                "organization_memberships": normalized_memberships,
                # Legacy single-org field — left here for back-compat with any
                # callers that still read it; new code should use memberships.
                "organization_id": payload.get("organization_id"),
            }
        except Exception as e:
            return {"error": f"Error decoding token: {e}"}

    def get_user_identity(self) -> dict:
        """
        Get the chatting user's identity from the delegation token.

        Returns the identity of the human user who is interacting with the
        agent through the chatbot. This is different from get_identity()
        which returns the agent's own identity.

        Returns:
            Dict with user_id, email, username, etc. or error dict.
        """
        token = self._get_effective_user_token()
        if not token:
            return {"error": "No user delegation token available"}

        try:
            payload = jwt.decode(token, options={"verify_signature": False})
            raw_memberships = payload.get("organization_memberships") or {}
            normalized_memberships = {}
            if isinstance(raw_memberships, dict):
                for k, v in raw_memberships.items():
                    try:
                        normalized_memberships[int(k)] = str(v)
                    except (TypeError, ValueError):
                        continue
            return {
                "user_id": payload.get("user_id"),
                "email": payload.get("email"),
                "username": payload.get("username"),
                "is_agent": False,
                "organization_memberships": normalized_memberships,
                "organization_id": payload.get("organization_id"),
                "delegated_by_agent_id": payload.get("delegated_by_agent_id"),
            }
        except Exception as e:
            return {"error": f"Error decoding user token: {e}"}

    def format_error(self, result: dict) -> str:
        """Format an error response with full details for debugging."""
        if "error" in result:
            status_code = result.get("status_code", "unknown")
            detail = result.get("detail", "")

            # Format detail based on type
            if isinstance(detail, dict):
                detail = json.dumps(detail, indent=2)
            elif isinstance(detail, str) and detail:
                # Try to parse as JSON for better formatting
                try:
                    parsed = json.loads(detail)
                    detail = json.dumps(parsed, indent=2)
                except (json.JSONDecodeError, TypeError):
                    pass  # Keep as string

            output = f"# Error (HTTP {status_code})\n\n"
            output += f"**Message**: {result['error']}\n\n"
            if detail:
                output += f"**Detail**:\n```\n{detail}\n```\n"
            else:
                output += "**Detail**: No additional details available\n"

            return output
        return ""

    def format_list(self, items: list, formatter: callable) -> str:
        """Format a list of items."""
        if not items:
            return "No items found."
        return "\n".join(formatter(item) for item in items)


def unwrap_list(result):
    """
    Normalize a paginated/non-paginated API response to a plain list.

    DRF endpoints with pagination return `{"results": [...], "count": N}`.
    Endpoints with `pagination_class = None` return a plain list. Callers
    used to do `result.get("results", result if isinstance(result, list) else [])`
    which AttributeErrors on lists (lists don't have `.get`). Use this helper
    instead — it branches safely on type before calling .get().
    """
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("results", [])
    return []


def is_api_error(result) -> bool:
    """Detect an error response from `api_request()` without falsely matching
    list responses that happen to contain the string 'error' as an element."""
    return isinstance(result, dict) and "error" in result


class ShizuhaTokenVerifier:
    """
    FastMCP TokenVerifier for shizuha-id-issued bearer tokens.

    Validates the incoming bearer at the MCP transport layer so FastMCP can
    return a real HTTP 401 + `WWW-Authenticate: Bearer resource_metadata=...`
    on a missing/expired/invalid token. That transport-level 401 is what makes
    an OAuth-aware MCP client refresh its access token via the authorization
    server and retry.

    This is NOT the authorization boundary — the downstream Django service still
    enforces real per-object permissions on every api_request. The verifier's
    only job is liveness: "is this a structurally valid, unexpired shizuha-id
    token?" so the client knows when to refresh.

    PLAT-1145: MCP transport bearer auth accepts only Shizuha ID RS256/JWKS
    access tokens. Shared-secret HS256 bearer verification is intentionally not
    attempted here; app SECRET_KEY/JWT_SECRET_KEY remain unrelated app secrets.
    """

    def __init__(self, service_name: str, issuer: str):
        self._service_name = service_name
        self._issuer = issuer.rstrip("/")
        self._jwks_client = None  # lazy PyJWKClient for RS256
        self._logger = logging.getLogger(f"shizuha-{service_name}-auth")

    def _decode(self, token: str) -> Optional[dict]:
        try:
            if self._jwks_client is None:
                from jwt import PyJWKClient

                self._jwks_client = PyJWKClient(
                    f"{self._issuer}/.well-known/jwks.json"
                )
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                options={"verify_aud": False, "verify_iss": False},
            )
        except jwt.ExpiredSignatureError:
            return None
        except Exception as exc:  # JWKS fetch failure / invalid token / non-RS256 token
            self._logger.debug(f"RS256 verify failed: {exc}")
            return None

    async def verify_token(self, token: str):
        """Return an AccessToken if the bearer is valid+unexpired, else None."""
        from mcp.server.auth.provider import AccessToken

        payload = self._decode(token)
        if payload is None:
            return None

        return AccessToken(
            token=token,
            client_id=str(
                payload.get("client_id")
                or payload.get("username")
                or payload.get("user_id")
                or "shizuha"
            ),
            scopes=["*"],
            expires_at=payload.get("exp"),
        )


_CANONICAL_OAUTH_ISSUER = "https://shizuha.com/id"
_CANONICAL_MCP_BASE_URL = "https://shizuha.com/mcp"


def _oauth_issuer() -> str:
    """Resolve the shizuha-id OAuth issuer URL (shared by discovery + verifier)."""
    issuer = os.environ.get("SHIZUHA_OAUTH_ISSUER", "").rstrip("/")
    if issuer:
        return issuer
    # OAuth/JWKS clients must receive the final HTTPS URL directly. The former
    # HTTP default now returns 308, which Python 3.10 PyJWKClient does not follow.
    return _CANONICAL_OAUTH_ISSUER


def _enforce_oauth() -> bool:
    """Whether to enforce transport-level bearer auth (per-service opt-in)."""
    return os.environ.get("SHIZUHA_MCP_OAUTH_ENFORCE", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _resource_server_url(service_name: str) -> str:
    """Public URL clients use to reach THIS MCP server (the protected resource).

    FastMCP appends `/.well-known/oauth-protected-resource` to this when
    building the WWW-Authenticate challenge, so it must be the externally
    reachable base the agent connected to.
    """
    explicit = os.environ.get("SHIZUHA_MCP_RESOURCE_URL", "").rstrip("/")
    if explicit:
        return explicit
    return f"{_CANONICAL_MCP_BASE_URL}/{service_name}"


def _register_oauth_discovery_routes(mcp_instance, service_name: str):
    """
    Advertise OAuth 2.0 metadata so clients can auto-discover the auth server.

    - /.well-known/oauth-protected-resource (RFC 9728): points at shizuha-id.
    - /.well-known/oauth-authorization-server (RFC 8414): path-aware fallback
      returning shizuha-id's metadata for clients that skip RFC 9728.
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    issuer = _oauth_issuer()

    @mcp_instance.custom_route('/.well-known/oauth-protected-resource', methods=['GET'])
    async def oauth_protected_resource(request: Request):
        scheme = request.headers.get('x-forwarded-proto') or request.url.scheme
        host = request.headers.get('host') or request.url.netloc
        # When fronted by nginx, X-Forwarded-Prefix carries the public path
        # (e.g. "/mcp/pulse"). Direct-port access has no prefix; default "/mcp".
        prefix = request.headers.get('x-forwarded-prefix', '/mcp')
        return JSONResponse({
            'resource': f'{scheme}://{host}{prefix}',
            'authorization_servers': [issuer],
            'bearer_methods_supported': ['header'],
            'scopes_supported': ['*'],
            'resource_name': f'shizuha-{service_name}',
        })

    _register_oauth_authorization_server_alias(mcp_instance, issuer)


def setup_oauth_routes(mcp_instance, service_name: str, logger=None):
    """Wire OAuth for an MCP server instance (call before mcp.run()).

    When SHIZUHA_MCP_OAUTH_ENFORCE is truthy, enables transport-level bearer
    enforcement: sets mcp.settings.auth + mcp._token_verifier AFTER FastMCP
    construction (FastMCP reads them lazily in streamable_http_app(), so this
    needs no change to the `mcp = FastMCP(...)` line). FastMCP then adds the
    BearerAuth middleware, wraps the app in RequireAuthMiddleware (→ 401 +
    `WWW-Authenticate: Bearer resource_metadata=...`), and serves
    /.well-known/oauth-protected-resource itself — so we register only the
    AS-metadata alias. Otherwise, registers the discovery routes (no auth).

    Reused by base.run_mcp_server AND custom entrypoints like pulse_server.py.
    """
    if _enforce_oauth():
        from mcp.server.auth.settings import AuthSettings

        issuer = _oauth_issuer()
        resource_url = _resource_server_url(service_name)
        mcp_instance.settings.auth = AuthSettings(
            issuer_url=issuer,
            resource_server_url=resource_url,
            required_scopes=[],
        )
        mcp_instance._token_verifier = ShizuhaTokenVerifier(service_name, issuer)
        if logger:
            logger.info(
                f"OAuth bearer enforcement ENABLED (issuer={issuer}, "
                f"resource={resource_url})"
            )
        _register_oauth_authorization_server_alias(mcp_instance, issuer)
    else:
        _register_oauth_discovery_routes(mcp_instance, service_name)


def _register_oauth_authorization_server_alias(mcp_instance, issuer: str):
    """Path-aware /.well-known/oauth-authorization-server alias returning
    shizuha-id's AS metadata, for clients that skip RFC 9728 discovery."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp_instance.custom_route('/.well-known/oauth-authorization-server', methods=['GET'])
    async def oauth_authorization_server_alias(request: Request):
        return JSONResponse({
            'issuer': issuer,
            'authorization_endpoint': f'{issuer}/oauth/authorize',
            'token_endpoint': f'{issuer}/api/oauth/token',
            'registration_endpoint': f'{issuer}/api/oauth/register',
            'userinfo_endpoint': f'{issuer}/api/oauth/userinfo',
            'revocation_endpoint': f'{issuer}/api/oauth/revoke',
            'scopes_supported': ['*', 'openid', 'profile', 'email'],
            'response_types_supported': ['code'],
            'grant_types_supported': ['authorization_code', 'refresh_token', 'password'],
            'code_challenge_methods_supported': ['S256', 'plain'],
            'token_endpoint_auth_methods_supported': ['none', 'client_secret_post'],
        })


def _is_stateless_http() -> bool:
    """
    Return True if stateless-HTTP mode is requested.

    Priority: SHIZUHA_MCP_STATELESS_HTTP env var > --stateless-http CLI flag.
    When True, FastMCP runs without server-side session state so any replica
    can handle any request — safe to run without ClientIP sessionAffinity.
    """
    env_val = os.environ.get("SHIZUHA_MCP_STATELESS_HTTP", "").lower()
    if env_val in ("1", "true", "yes", "on"):
        return True
    if env_val in ("0", "false", "no", "off"):
        # Explicit env-level false overrides any CLI flag.
        return False
    return "--stateless-http" in sys.argv


def run_mcp_server(mcp_instance, base: ShizuhaMCPBase):
    """
    Standard server startup for all MCP servers.

    Supports three transport modes selected via CLI flags:
      --http-port PORT   Streamable HTTP (preferred for persistent daemons)
      --sse-port PORT    Legacy SSE (deprecated, use --http-port)
      (no flag)          stdio (for local subprocess / MCP inspector)

    Stateless-HTTP mode (PLAT-612):
      Set SHIZUHA_MCP_STATELESS_HTTP=true (or --stateless-http) to run FastMCP
      with stateless_http=True.  In this mode no server-side session state is
      kept, so requests can be load-balanced across replicas without ClientIP
      affinity.  The Helm chart controls the matching sessionAffinity toggle.
    """
    base.logger.info(f"Starting Shizuha {base.service_name.title()} MCP server")
    base.logger.info(f"API URL: {base.api_url}")

    # Wire OAuth: either transport-level bearer enforcement (when
    # SHIZUHA_MCP_OAUTH_ENFORCE=true) or just discovery routes. Centralized in
    # setup_oauth_routes so custom entrypoints (e.g. pulse_server.py) reuse it.
    setup_oauth_routes(mcp_instance, base.service_name, logger=base.logger)

    if "--http-port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--http-port") + 1])
        host = "0.0.0.0"
        stateless = _is_stateless_http()
        base.logger.info(f"Streamable HTTP transport on {host}:{port} (stateless={stateless})")
        mcp_instance.settings.host = host
        mcp_instance.settings.port = port
        # Disable DNS rebinding protection — these are internal services
        # behind Docker networking / Tailscale, not public-facing.
        # FastMCP auto-enables it for localhost bindings in __init__,
        # but we bind to 0.0.0.0 and need to accept any Host header.
        mcp_instance.settings.transport_security = None
        mcp_instance.run(transport="streamable-http", stateless_http=stateless)
    elif "--sse-port" in sys.argv:
        # Legacy SSE support (deprecated)
        port = int(sys.argv[sys.argv.index("--sse-port") + 1])
        base.logger.info(f"SSE transport on 0.0.0.0:{port} (deprecated, use --http-port)")
        mcp_instance.settings.host = "0.0.0.0"
        mcp_instance.settings.port = port
        mcp_instance.settings.transport_security = None
        mcp_instance.run(transport="sse")
    else:
        mcp_instance.run(transport="stdio")
