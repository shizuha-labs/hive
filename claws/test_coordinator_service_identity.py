"""Canonical revocable coordinator service identity, distinct from user sessions."""
from types import SimpleNamespace
from django.test import SimpleTestCase
from hive_platform import coordinator
from hive_platform.models import TokenPool
from claws.middleware import _is_coordinator_broker_claims

CLIENT = 'hive-coordinator-broker'
SCOPE = 'hive-coordinator:model-token'


def service_claims(**changes):
    claims = dict(token_type='access', client_id=CLIENT, sub='service:' + CLIENT,
                  is_service=True, aud=['hive-coordinator'], scope=SCOPE)
    claims.update(changes)
    return claims


class CoordinatorServiceIdentityTests(SimpleTestCase):
    def test_canonical_client_is_broker_and_has_only_existing_org_owner(self):
        request = SimpleNamespace(auth=service_claims(), user=SimpleNamespace(id=0))
        self.assertTrue(coordinator.is_broker_request(request))
        self.assertTrue(_is_coordinator_broker_claims(request.auth))
        owner = coordinator.resolve_owner(request)
        self.assertEqual((owner.owner_kind, owner.owner_id), ('org', TokenPool.ORG_OWNER_ID))
        self.assertEqual(coordinator.broker_identity(request), CLIENT)

    def test_wrong_client_role_audience_subject_or_extra_scope_cannot_gain_authority(self):
        for changes in [dict(client_id='hive-pulse-fleet-read'), dict(is_service=False),
                        dict(aud=['hive']), dict(sub='service:other'),
                        dict(scope=SCOPE+' hive:admin.write')]:
            with self.subTest(changes=changes):
                claims = service_claims(**changes)
                self.assertFalse(coordinator.is_broker_request(SimpleNamespace(auth=claims)))
                self.assertFalse(_is_coordinator_broker_claims(claims))

    def test_service_cannot_select_a_user_or_foreign_org_pool_via_claims(self):
        for owner in [('user', '123'), ('org', 'foreign-org')]:
            claims = service_claims(owner_kind=owner[0], owner_id=owner[1])
            request = SimpleNamespace(auth=claims, user=SimpleNamespace(id=0))
            self.assertFalse(coordinator.is_broker_request(request))
            resolved = coordinator.resolve_owner(request)
            self.assertNotEqual((resolved.owner_kind, resolved.owner_id), owner)

    def test_canonical_role_without_revocable_client_identity_is_denied(self):
        for changes in [dict(client_id=None), dict(client_id=''), dict(token_type='refresh')]:
            with self.subTest(changes=changes):
                self.assertFalse(coordinator.is_broker_request(SimpleNamespace(auth=service_claims(**changes))))

    def test_service_cannot_fall_back_to_legacy_role(self):
        claims = service_claims(scope='coordinator:model-token')
        self.assertFalse(coordinator.is_broker_request(SimpleNamespace(auth=claims)))

from unittest import mock
from django.test import override_settings
from kubernetes import client
from kubernetes.client.rest import ApiException
from fleet import provisioner
from fleet.models import FleetAgent


@override_settings(
    FLEET_COORDINATOR_MODEL_TOKEN_URL='http://hive/coordinator/model-token',
    FLEET_COORDINATOR_TOKEN_SECRET_NAME='old-static-bearer',
    FLEET_COORDINATOR_OAUTH_SECRET_NAME='hive-coordinator-broker-oauth',
)
class CoordinatorOAuthRenderingTests(SimpleTestCase):
    def setUp(self):
        self.agent = FleetAgent(agent_username='oauth-seat', agent_id='oauth-runtime',
                               image='runtime:reviewed', sidecar_set=['broker'])
        self.agent._reviewed_runtime_image = self.agent.image

    def test_oauth_credentials_are_broker_only_and_static_bearer_is_absent(self):
        broker = provisioner._broker_sidecar(self.agent)
        env = {e.name: e for e in broker.env}
        self.assertNotIn('MCP_AUTH_PROXY_COORDINATOR_TOKEN', env)
        for suffix, key in [('CLIENT_ID', 'client-id'), ('CLIENT_SECRET', 'client-secret'),
                            ('TOKEN_URL', 'token-url')]:
            item = env['MCP_AUTH_PROXY_COORDINATOR_OAUTH_' + suffix]
            self.assertIsNone(item.value)
            ref = item.value_from.secret_key_ref
            self.assertEqual((ref.name, ref.key, ref.optional),
                             ('hive-coordinator-broker-oauth', key, False))
        self.assertEqual(env['MCP_AUTH_PROXY_COORDINATOR_OAUTH_SCOPE'].value, SCOPE)
        self.agent.env = {'MCP_AUTH_PROXY_COORDINATOR_OAUTH_CLIENT_SECRET': 'untrusted-override'}
        agent = provisioner._agent_container(self.agent, has_broker=True, has_dind=False)
        self.assertFalse(any('COORDINATOR_OAUTH' in e.name for e in agent.env))
        self.assertFalse(any(e.value_from and e.value_from.secret_key_ref and
                             e.value_from.secret_key_ref.name == 'hive-coordinator-broker-oauth'
                             for e in agent.env))

    def test_producer_convergence_replaces_legacy_mode_once_without_extra_authority(self):
        with override_settings(FLEET_COORDINATOR_OAUTH_SECRET_NAME=''):
            observed = provisioner._broker_sidecar(self.agent)
        observed.env.append(client.V1EnvVar(name='UNRELATED', value='preserved'))
        desired = provisioner._broker_sidecar(self.agent)
        self.assertTrue(provisioner._converge_broker_producer_contract(observed, desired))
        self.assertFalse(any(e.name == 'MCP_AUTH_PROXY_COORDINATOR_TOKEN' for e in observed.env))
        self.assertEqual(next(e.value for e in observed.env if e.name == 'UNRELATED'), 'preserved')
        self.assertFalse(provisioner._converge_broker_producer_contract(observed, desired))

    def test_isolated_namespace_projects_only_new_managed_client_secret(self):
        core = mock.Mock()
        import base64
        data = {key: base64.b64encode(value.encode()).decode() for key, value in {
            'client-id': CLIENT, 'client-secret': 'test-only-broker-client-secret-0123456789',
            'token-url': 'http://shizuha-id.shizuha.svc.cluster.local:8001/api/oauth/token',
        }.items()}
        source = client.V1Secret(type='Opaque', data=data)
        def read(name, ns):
            self.assertEqual(name, 'hive-coordinator-broker-oauth')
            if ns == 'shizuha-hive':
                return source
            raise ApiException(status=404)
        core.read_namespaced_secret.side_effect = read
        self.assertTrue(provisioner._ensure_coordinator_broker_token(core, 'fleet-oauth-seat', self.agent))
        created = core.create_namespaced_secret.call_args.args[1]
        self.assertEqual(created.metadata.name, 'hive-coordinator-broker-oauth')
        self.assertEqual(created.data, data)
        self.assertEqual(created.metadata.labels['app.kubernetes.io/managed-by'], 'shizuha-hive')
        core.read_namespaced_secret.side_effect = ApiException(status=403)
        core.create_namespaced_secret.reset_mock()
        with self.assertRaises(ApiException):
            provisioner._ensure_coordinator_broker_token(core, 'fleet-oauth-seat', self.agent)
        core.create_namespaced_secret.assert_not_called()

    def test_shared_fleet_namespace_does_not_skip_oauth_projection(self):
        with mock.patch.object(provisioner, '_ensure_coordinator_oauth_secret', return_value=True) as project:
            self.assertTrue(provisioner._ensure_coordinator_broker_token(
                mock.Mock(), provisioner.fleet_namespace(), self.agent))
        self.assertEqual(project.call_args.args[1], provisioner.fleet_namespace())

    def test_existing_wrong_client_projection_is_never_replaced_or_adopted(self):
        import base64
        source = client.V1Secret(data={key: base64.b64encode(value.encode()).decode()
            for key, value in {'client-id': CLIENT,
                'client-secret': 'test-only-broker-client-secret-0123456789',
                'token-url': 'http://shizuha-id.shizuha.svc.cluster.local:8001/api/oauth/token'}.items()})
        core = mock.Mock()
        core.read_namespaced_secret.side_effect = [source, client.V1Secret(data={'client-id': 'foreign'})]
        with self.assertRaisesRegex(RuntimeError, 'differs from canonical source'):
            provisioner._ensure_coordinator_broker_token(core, 'fleet-oauth-seat', self.agent)
        core.create_namespaced_secret.assert_not_called()

from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
from django.test import TestCase
from rest_framework.test import APIRequestFactory
from claws.authentication import FederatedJWTAuthentication
from hive_platform import token_revocation
from hive_platform.coordinator_views import ModelTokenView
from hive_platform.models import TokenEntry


class CoordinatorServiceBoundaryTests(TestCase):
    """Real JWT verification, revocation decision, role gate, and owner lookup."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        token_revocation._REVOCATION_CACHE.clear()
        for kind, owner, material in [('org', TokenPool.ORG_OWNER_ID, 'test-existing-org-token'),
                                      ('user', '987654321', 'test-foreign-user-token')]:
            pool = TokenPool.objects.create(owner_kind=kind, owner_id=owner, provider='anthropic')
            entry = TokenEntry(pool=pool, label='service-boundary', priority=1)
            entry.set_token(material)
            entry.save()
        jwks = mock.Mock()
        jwks.get_signing_key_from_jwt.return_value.key = self.key.public_key()
        patcher = mock.patch.object(FederatedJWTAuthentication, '_jwks_client', return_value=jwks)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(token_revocation._REVOCATION_CACHE.clear)

    def call(self, claims=None, *, active=True):
        now = datetime.now(timezone.utc)
        payload = service_claims(iat=int(now.timestamp()), exp=int((now+timedelta(minutes=15)).timestamp()),
                                 jti='test-only-jti')
        payload.update(claims or {})
        bearer = jwt.encode(payload, self.key, algorithm='RS256', headers={'kid': 'test-service-key'})
        request = APIRequestFactory().post('/coordinator/model-token', {
            'provider': 'anthropic', 'owner_kind': 'user', 'owner_id': '987654321',
            'preferred_entry_id': '00000000-0000-0000-0000-000000000000',
        }, format='json', HTTP_AUTHORIZATION='Bearer '+bearer)
        with mock.patch.object(token_revocation, '_lookup_revocation', return_value={'active': active}) as lookup:
            response = ModelTokenView.as_view()(request)
        lookup.assert_called_once_with(bearer)
        return response

    def test_registered_service_uses_existing_org_authority_and_ignores_body_owner(self):
        response = self.call()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['token'], 'test-existing-org-token')
        self.assertNotIn('test-foreign-user-token', str(response.data))

    def test_revoked_canonical_service_is_refused_before_coordinator_lookup(self):
        response = self.call(active=False)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('token', response.data)

    def test_valid_signed_wrong_client_or_owner_claim_cannot_select_foreign_pool(self):
        for claims in [dict(client_id='hive-pulse-fleet-read'),
                       dict(owner_kind='user', owner_id='987654321'),
                       dict(owner_kind='org', owner_id='foreign-org')]:
            with self.subTest(claims=claims):
                token_revocation._REVOCATION_CACHE.clear()
                response = self.call(claims)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn('token', response.data)

    def signed_bearer(self, **changes):
        now = datetime.now(timezone.utc)
        payload = service_claims(iat=int(now.timestamp()), exp=int((now+timedelta(minutes=15)).timestamp()),
                                 jti='test-only-route-boundary')
        payload.update(changes)
        return jwt.encode(payload, self.key, algorithm='RS256', headers={'kid': 'test-service-key'})

    def test_signed_client_http_boundary_denies_all_owner_crud_and_unrelated_routes(self):
        from rest_framework.test import APIClient
        http = APIClient()
        http.credentials(HTTP_AUTHORIZATION='Bearer '+self.signed_bearer())
        entry = TokenEntry.objects.filter(pool__owner_kind='org').get()
        before = list(TokenEntry.objects.values('id', 'label', 'active'))
        base = '/hive/api/v1/coordinator/token-entries'
        cases = [('get', base), ('post', base), ('get', base+'/'+str(entry.id)),
                 ('patch', base+'/'+str(entry.id)), ('delete', base+'/'+str(entry.id)),
                 ('get', '/hive/api/v1/fleet/agents'), ('get', '/hive/api/v1/me'),
                 ('post', '/internal/v1/accounts:resolve'),
                 ('get', '/hive/api/v1/coordinator/model-token'),
                 ('options', '/hive/api/v1/coordinator/model-token')]
        with mock.patch.object(token_revocation, '_lookup_revocation', return_value={'active': True}):
            for method, path in cases:
                with self.subTest(method=method, path=path):
                    response = getattr(http, method)(path, {}, format='json')
                    self.assertEqual(response.status_code, 404)
            response = http.post('/hive/api/v1/coordinator/model-token',
                                 {'provider': 'anthropic'}, format='json')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data['token'], 'test-existing-org-token')
        self.assertEqual(list(TokenEntry.objects.values('id', 'label', 'active')), before)

    def test_direct_owner_views_deny_signed_client_without_middleware(self):
        from hive_platform.coordinator_views import TokenEntryListCreateView, TokenEntryDetailView
        entry = TokenEntry.objects.filter(pool__owner_kind='org').get()
        factory = APIRequestFactory()
        for view, method, kwargs in [(TokenEntryListCreateView, 'get', {}),
                                     (TokenEntryListCreateView, 'post', {}),
                                     (TokenEntryDetailView, 'get', {'entry_id': entry.id}),
                                     (TokenEntryDetailView, 'patch', {'entry_id': entry.id}),
                                     (TokenEntryDetailView, 'delete', {'entry_id': entry.id})]:
            with self.subTest(view=view.__name__, method=method):
                request = getattr(factory, method)('/coordinator/token-entries', {}, format='json',
                    HTTP_AUTHORIZATION='Bearer '+self.signed_bearer())
                with mock.patch.object(token_revocation, '_lookup_revocation', return_value={'active': True}):
                    self.assertEqual(view.as_view()(request, **kwargs).status_code, 404)

    def test_signed_client_middleware_allows_only_three_post_operations_and_aliases(self):
        from claws.middleware import ServiceAccessMiddleware
        from django.http import JsonResponse
        from django.test import RequestFactory
        reached = []
        boundary = ServiceAccessMiddleware(lambda request: reached.append(request.path) or JsonResponse({'ok': True}))
        for prefix in ['/hive/api/', '/api/']:
            for route in ['v1/coordinator/model-token', 'v1/coordinator/report-status',
                          'v1/fleet/agents/test-seat/mcp-projection']:
                for suffix in ['', '/']:
                    path = prefix+route+suffix
                    with self.subTest(path=path):
                        request = RequestFactory().post(path, HTTP_AUTHORIZATION='Bearer '+self.signed_bearer())
                        with mock.patch.object(token_revocation, '_lookup_revocation', return_value={'active': True}):
                            response = boundary(request)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(reached[-1], path)
        # Both transports accepted by Hive must enforce the same service boundary.
        for cookie in ['access_token', 'shizuha-access-token']:
            for path in ['/internal/v1/accounts:resolve', '/hive/api/v1/fleet/agents']:
                request = RequestFactory().post(path)
                request.COOKIES[cookie] = self.signed_bearer()
                with mock.patch.object(token_revocation, '_lookup_revocation', return_value={'active': True}):
                    self.assertEqual(boundary(request).status_code, 404)
            for route in ['v1/coordinator/model-token', 'v1/coordinator/report-status',
                          'v1/fleet/agents/test-seat/mcp-projection']:
                request = RequestFactory().post('/hive/api/'+route)
                request.COOKIES[cookie] = self.signed_bearer()
                with mock.patch.object(token_revocation, '_lookup_revocation', return_value={'active': True}):
                    self.assertEqual(boundary(request).status_code, 200)
        for claims in [dict(scope=SCOPE+' hive:admin.write'), dict(owner_id='foreign-org'),
                       dict(is_staff=True, scope='hive:admin.write')]:
            with self.subTest(claims=claims):
                request = RequestFactory().post('/hive/api/v1/coordinator/model-token',
                    HTTP_AUTHORIZATION='Bearer '+self.signed_bearer(**claims))
                with mock.patch.object(token_revocation, '_lookup_revocation', return_value={'active': True}):
                    self.assertEqual(boundary(request).status_code, 404)


class CoordinatorServiceProjectionTests(CoordinatorServiceBoundaryTests):
    """Actual signed HTTP route must not forward its Hive-only bearer to Pulse."""

    def projection_call(self, *, rows=None, fresh_error=None, claims=None, active=True,
                        runtime_id="runtime-projection-oauth", runtime_route=False, extra_fields=None, username_alias=False):
        import base64
        import json
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from django.utils import timezone as django_timezone
        from rest_framework.test import APIClient
        from fleet.models import AgentMcpProjectionState, BASE_PLATFORM_MCP_SERVERS
        from fleet.mcp_projection import AUTHORITY_CURRENT, PROTOCOL_PREFIX, projection_fingerprint, projection_service_identity
        from fleet import views
        agent = FleetAgent.objects.create(agent_username='projection-oauth', agent_id=runtime_id)
        from fleet.effective_capabilities import compute_effective_capability_grant_for_memberships
        grant = compute_effective_capability_grant_for_memberships(rows or [],
            {str(agent.id), agent.agent_username, agent.agent_username+'@shizuha.com'})
        services, revision = projection_service_identity(grant)
        AgentMcpProjectionState.objects.create(agent=agent,
            fingerprint=projection_fingerprint(services, revision),
            generation=1, current_high_water=1, authority_state=AUTHORITY_CURRENT,
            lease_deadline=django_timezone.now()+timedelta(seconds=20))
        key = Ed25519PrivateKey.generate()
        signing = SimpleNamespace(kid='projection-test', private_key=key)
        http = APIClient()
        http.credentials(HTTP_AUTHORIZATION='Bearer '+self.signed_bearer(**(claims or {})))
        rejected = mock.Mock(status_code=401)
        rejected.json.return_value = {'detail': 'Token has wrong audience for Pulse'}
        with mock.patch.object(token_revocation, '_lookup_revocation', return_value={'active': active}), \
             mock.patch('fleet.capability_evidence._fresh_memberships',
                        return_value=(rows or [], 0, 100), side_effect=fresh_error) as fresh, \
             mock.patch.object(views.requests, 'request', return_value=rejected) as forwarded, \
             mock.patch('fleet.mcp_projection.load_projection_signing_identity', return_value=signing), \
             mock.patch.object(views, '_nondb_agent', return_value={
                 'agent_username': agent.agent_username, 'email': agent.agent_username+'@shizuha.com',
                 'id': agent.agent_username} if username_alias else None):
            route_id = agent.agent_username if username_alias else (agent.agent_id if runtime_route else str(agent.id))
            body = {'nonce': base64.urlsafe_b64encode(b'n'*32).decode().rstrip('=')}
            body.update(extra_fields or {})
            response = http.post(f'/hive/api/v1/fleet/agents/{route_id}/mcp-projection', body, format='json')
        if response.status_code == 200:
            envelope = json.loads(response.content)
            preimage = PROTOCOL_PREFIX+b'\0'+b'n'*32+b'\0'+json.dumps(
                envelope['projection'], sort_keys=True, separators=(',', ':')).encode()
            key.public_key().verify(base64.urlsafe_b64decode(envelope['signature']+'=='), preimage)
            self.assertEqual(envelope['projection']['agent_audience'], agent.agent_id or str(agent.id))
            state = AgentMcpProjectionState.objects.get(agent=agent)
            self.assertEqual(state.agent_id, agent.id)
            self.assertEqual(AgentMcpProjectionState.objects.count(), 1)
        return response, fresh, forwarded

    def test_runtime_route_binds_runtime_identity_not_hive_primary_key(self):
        response, _, _ = self.projection_call(
            runtime_id='22724ca5-48b5-50a8-9b44-9dcd78a13b01', runtime_route=True,
            extra_fields={'agent_audience': 'attacker-selected', 'agent_id': 'foreign-seat'})
        self.assertEqual(response.status_code, 200)

    def test_resolved_runtime_username_alias_cannot_become_signed_audience(self):
        response, _, _ = self.projection_call(username_alias=True,
            extra_fields={'agent_audience': 'foreign-seat'})
        self.assertEqual(response.status_code, 200)

    def test_native_row_without_runtime_identity_binds_its_primary_key(self):
        response, _, _ = self.projection_call(runtime_id='')
        self.assertEqual(response.status_code, 200)

    def test_signed_projection_uses_server_roster_without_forwarding_hive_bearer(self):
        rows = [dict(team_slug='engineering', organization_slug='shizuha'),
                dict(team_slug='engineering', organization_slug='customer-x')]
        response, fresh, forwarded = self.projection_call(rows=rows)
        self.assertEqual(response.status_code, 200, getattr(response, 'data', None))
        fresh.assert_called_once_with('projection-oauth@shizuha.com')
        forwarded.assert_not_called()

    def test_projection_stale_server_roster_fails_closed_without_user_token_fallback(self):
        from fleet.capability_evidence import CapabilityEvidenceError
        response, fresh, forwarded = self.projection_call(fresh_error=CapabilityEvidenceError(
            'HIVE_MATERIALIZATION_ROSTER_STALE', http_status=503))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['error'], 'HIVE_MATERIALIZATION_ROSTER_STALE')
        forwarded.assert_not_called()

    def test_projection_wrong_client_and_revoked_identity_never_read_server_roster(self):
        for claims, active in [(dict(client_id='hive-pulse-fleet-read'), True), ({}, False)]:
            with self.subTest(claims=claims, active=active):
                token_revocation._REVOCATION_CACHE.clear()
                response, fresh, forwarded = self.projection_call(claims=claims, active=active)
                self.assertIn(response.status_code, (403, 404))
                fresh.assert_not_called()
                forwarded.assert_not_called()
                FleetAgent.objects.filter(agent_username='projection-oauth').delete()


    def test_foreign_same_name_membership_cannot_unlock_internal_team_credential(self):
        from fleet import views
        from fleet.models import FleetAgentTeamCredentialEligibility, HiveCredential
        agent = FleetAgent.objects.create(agent_username='projection-org-scope')
        FleetAgentTeamCredentialEligibility.objects.create(agent=agent,
            organization_slug='shizuha', team_slug='qa', is_active=True, actor_user_id='test')
        credential = HiveCredential.objects.create(scope=HiveCredential.Scope.TEAM,
            organization_slug='shizuha', team_slug='qa', provider=HiveCredential.Provider.GENERIC_ENV,
            purpose='TEST_ORG_SCOPE', secret_ref='k8s-secret://test/qa#TEST_ORG_SCOPE',
            created_by_user_id='test')
        request = SimpleNamespace(auth=service_claims(), META={}, query_params={}, GET={})
        rows = [dict(team_slug='engineering', organization_slug='shizuha'),
                dict(team_slug='qa', organization_slug='customer-x')]
        from fleet.effective_capabilities import compute_effective_capability_grant_for_memberships
        with mock.patch('fleet.capability_evidence._fresh_memberships', return_value=(rows, 0, 100)), \
             mock.patch('fleet.effective_capabilities.compute_effective_capability_grant_for_memberships',
                        wraps=compute_effective_capability_grant_for_memberships) as compute, \
             mock.patch.object(views, '_pulse_roster_memberships') as forwarded:
            payload, error = views._effective_capability_set(request, str(agent.id))
            self.assertIsNone(error)
            self.assertEqual(compute.call_args.args[0], rows)
            self.assertEqual(payload['source_team_memberships'], [
                dict(organization_slug='customer-x', team_slug='qa'),
                dict(organization_slug='shizuha', team_slug='engineering')])
            self.assertNotIn(str(credential.id), [row['grant_id'] for row in payload['credential_materializations']])
            # Only an authoritative internal QA placement satisfies the internal eligibility.
            rows.append(dict(team_slug='qa', organization_slug='shizuha'))
            payload, error = views._effective_capability_set(request, str(agent.id))
            self.assertIsNone(error)
            self.assertIn(str(credential.id), [row['grant_id'] for row in payload['credential_materializations']])
            forwarded.assert_not_called()

    def test_ordinary_human_and_legacy_daemon_roster_sources_are_unchanged(self):
        from fleet import views
        agent = FleetAgent.objects.create(agent_username='projection-legacy')
        error = object()
        for daemon in (False, True):
            request = SimpleNamespace(auth={'user_id': 123}, META={}, query_params={}, GET={})
            with self.subTest(daemon=daemon), \
                 mock.patch.object(views, '_request_has_valid_daemon_credential', return_value=daemon), \
                 mock.patch.object(views, '_auth_headers_for_admin', return_value={} if daemon else {'Authorization': 'Bearer user'}), \
                 mock.patch.object(views, '_pulse_roster_memberships', return_value=(None, None, error)) as human, \
                 mock.patch.object(views, '_cached_pulse_roster_memberships_for_daemon', return_value=(None, None, error)) as legacy, \
                 mock.patch('fleet.capability_evidence._fresh_memberships') as canonical:
                self.assertEqual(views._effective_capability_set(request, str(agent.id)), (None, error))
                self.assertEqual(human.call_count, int(not daemon))
                self.assertEqual(legacy.call_count, int(daemon))
                canonical.assert_not_called()
