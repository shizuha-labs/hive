import ast
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from claws.views import MeView


class HiveMcpIdentityRouteTests(SimpleTestCase):
    def call_actual_tool(self, *, authenticated):
        source = Path(__file__).resolve().parents[1] / 'mcp-server/hive_server.py'
        module = ast.parse(source.read_text(), filename=str(source))
        function = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'hive_get_my_identity')
        function.decorator_list = []
        self.calls = []

        def api_request(method, endpoint):
            path = '/api/' + endpoint
            self.calls.append((method, path))
            resolved = resolve(path)
            self.assertIs(resolved.func.view_class, MeView)
            request = APIRequestFactory().get(path)
            if authenticated:
                force_authenticate(request, user=SimpleNamespace(
                    id=698, username='hina', email='hina@shizuha.com',
                    is_staff=False, is_superuser=False, is_authenticated=True,
                    enabled_services=['hive'],
                ))
            with mock.patch('fleet.views._active_jit_grant_for_scope', return_value=(None, 'none', '')):
                response = resolved.func(request, **resolved.kwargs)
            self.response_status = response.status_code
            if response.status_code >= 400:
                return {'error': 'authentication rejected', 'status_code': response.status_code}
            return response.data

        namespace = {
            'get_base': lambda: SimpleNamespace(api_request=api_request),
            '_err_text': lambda result: result['error'],
        }
        compiled = compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec')
        exec(compiled, namespace)
        return namespace['hive_get_my_identity']()

    def test_actual_tool_body_resolves_and_returns_nonstaff_caller_identity(self):
        result = self.call_actual_tool(authenticated=True)
        self.assertEqual(self.calls, [('GET', '/api/v1/me/')])
        self.assertEqual(self.response_status, 200)
        self.assertTrue(result.startswith('Hive identity: '), result)
        identity = ast.literal_eval(result.removeprefix('Hive identity: '))
        self.assertEqual((identity['id'], identity['username']), (698, 'hina'))
        self.assertFalse(identity['is_staff'])
        self.assertFalse(identity['is_superuser'])

    def test_actual_tool_route_cannot_return_anonymous_identity(self):
        result = self.call_actual_tool(authenticated=False)
        self.assertIn(self.response_status, (401, 403))
        self.assertTrue(result.startswith('Could not resolve identity: '), result)
