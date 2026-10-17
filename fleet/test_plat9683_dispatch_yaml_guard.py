"""PLAT-9683 leg 4 — dispatch-side pre-validation of the target workflow YAML.

Covers the dispatch-side watchdog ni specified in PLAT-9683: a
duplicate-key-rejecting safe-load of the workflow file AT THE DISPATCH REF
before the dispatch POST fires, failing loudly with the decoder's exact
parse error — killing the 204-no-run / silent-daemon-death class
(PLAT-9660: duplicate bare ``workflow_dispatch`` key fatalized the runner's
intake decode pre-log; Forgejo still returned 204 and 9 silent restarts
followed).

Faces:
- guard validator: duplicate top-level key rejected with key + line + column;
- duplicate nested key rejected (the trap is general, not trigger-only);
- malformed YAML rejected; non-mapping doc rejected; valid workflow passes;
- fetch failure (404 / 5xx) fails closed with a named reason;
- wiring: guard failure blocks ``dispatch_agent_runtime_build`` (no POST,
  error surfaced); guard pass dispatches; env kill-switch restores pass-through.
"""
from unittest import mock

from django.test import TestCase

from fleet import workflow_dispatch_guard as guard
from fleet import harness_versions as hv


VALID_WORKFLOW = """\
name: Build Agent Runtime
on:
  workflow_dispatch:
    inputs:
      tag:
        description: image tag
  push:
    branches: [master]
jobs:
  build:
    runs-on: docker
    steps:
      - run: echo ok
"""

# The exact PLAT-9660 trigger shape: duplicate bare workflow_dispatch key.
DUPLICATE_TOPLEVEL = """\
name: Build Agent Runtime
on:
  workflow_dispatch:
    inputs:
      tag:
        description: image tag
  workflow_dispatch:
jobs:
  build:
    runs-on: docker
    steps:
      - run: echo ok
"""

DUPLICATE_NESTED = """\
name: Build Agent Runtime
on:
  workflow_dispatch:
    inputs:
      tag:
        description: image tag
        description: overwritten tag
jobs:
  build:
    runs-on: docker
    steps:
      - run: echo ok
"""

MALFORMED = "name: Build Agent Runtime\non: [unclosed\njobs:\n"

NON_MAPPING = "- just\n- a\n- list\n"


class StrictLoaderTests(TestCase):
    def test_duplicate_toplevel_key_rejected_with_key_and_marks(self):
        result = guard.validate_workflow_yaml(DUPLICATE_TOPLEVEL)
        self.assertFalse(result['ok'])
        self.assertIn('duplicate key', result['error'])
        self.assertIn("'workflow_dispatch'", result['error'])
        # decoder-style marks: line + column of the duplicate key node
        self.assertIn('line 7', result['error'])
        self.assertIn('column 3', result['error'])

    def test_duplicate_nested_key_rejected(self):
        result = guard.validate_workflow_yaml(DUPLICATE_NESTED)
        self.assertFalse(result['ok'])
        self.assertIn("duplicate key 'description'", result['error'])

    def test_malformed_yaml_rejected_with_parser_error(self):
        result = guard.validate_workflow_yaml(MALFORMED)
        self.assertFalse(result['ok'])
        self.assertIn('strict-decode failed', result['error'])

    def test_non_mapping_document_rejected(self):
        result = guard.validate_workflow_yaml(NON_MAPPING)
        self.assertFalse(result['ok'])
        self.assertIn('top-level document is list', result['error'])

    def test_valid_workflow_passes(self):
        self.assertEqual(guard.validate_workflow_yaml(VALID_WORKFLOW), {'ok': True})


class ValidateWorkflowDispatchYamlTests(TestCase):
    """End-to-end guard behavior against a mocked raw-content fetch."""

    def _validate(self, source=None, status=200, body=None):
        response = mock.Mock(status_code=status)
        response.text = source if source is not None else (body or '')
        with mock.patch.object(guard.requests, 'get', return_value=response):
            return guard.validate_workflow_dispatch_yaml(
                'shizuha-labs/shizuha-beta',
                'master',
                'build-agent-runtime.yml',
                api='http://forgejo-http.origin.svc.cluster.local',
                token='t',
            )

    def test_readable_valid_workflow_passes(self):
        result = self._validate(source=VALID_WORKFLOW)
        self.assertTrue(result['ok'])

    def test_duplicate_key_workflow_fails_with_guard_prefix(self):
        result = self._validate(source=DUPLICATE_TOPLEVEL)
        self.assertFalse(result['ok'])
        self.assertIn('workflow-dispatch guard: .forgejo/workflows/build-agent-runtime.yml at master:', result['error'])
        self.assertIn("duplicate key 'workflow_dispatch'", result['error'])

    def test_missing_workflow_fails_closed_named(self):
        result = self._validate(status=404, body='Not Found')
        self.assertFalse(result['ok'])
        self.assertIn('could not read .forgejo/workflows/build-agent-runtime.yml at master', result['error'])
        self.assertIn('HTTP 404', result['error'])

    def test_raw_api_server_error_fails_closed_named(self):
        result = self._validate(status=503, body='unavailable')
        self.assertFalse(result['ok'])
        self.assertIn('HTTP 503', result['error'])

    def test_kill_switch_disables_guard(self):
        response = mock.Mock(status_code=404, text='Not Found')
        with mock.patch.dict('os.environ', {'HIVE_WORKFLOW_DISPATCH_GUARD': '0'}):
            with mock.patch.object(guard.requests, 'get', return_value=response):
                result = guard.validate_workflow_dispatch_yaml(
                    'shizuha-labs/shizuha-beta', 'master', 'build-agent-runtime.yml',
                    api='http://api', token='t',
                )
        self.assertTrue(result['ok'])
        self.assertIn('guard disabled', result['skipped'])


class DispatchWiringTests(TestCase):
    """The dispatch caller must block loudly on guard failure, fire on pass."""

    def setUp(self):
        self.token_patcher = mock.patch.dict('os.environ', {'HIVE_FORGEJO_TOKEN': 'tok'})
        self.token_patcher.start()
        self.addCleanup(self.token_patcher.stop)

    def _dispatch(self, guard_result):
        post = mock.Mock(status_code=204, text='')
        with mock.patch.object(
            guard, 'validate_workflow_dispatch_yaml', return_value=guard_result
        ) as guard_mock, mock.patch.object(hv.requests, 'post', return_value=post) as post_mock:
            result = hv.dispatch_agent_runtime_build({'codex': '1.2.3'})
        return result, guard_mock, post_mock

    def test_guard_failure_blocks_dispatch_and_surfaces_parse_error(self):
        bad = {'ok': False, 'error': "workflow-dispatch guard: ... duplicate key 'workflow_dispatch' ... line 7, column 3"}
        result, guard_mock, post_mock = self._dispatch(bad)
        self.assertFalse(result['dispatched'])
        self.assertIn("duplicate key 'workflow_dispatch'", result['error'])
        post_mock.assert_not_called()
        guard_mock.assert_called_once()

    def test_guard_pass_fires_dispatch(self):
        result, _guard_mock, post_mock = self._dispatch({'ok': True})
        self.assertTrue(result['dispatched'])
        self.assertIn('tag', result)
        post_mock.assert_called_once()

    def test_fetch_failure_error_is_named_not_silent(self):
        bad = {'ok': False, 'error': 'workflow-dispatch guard: could not read ... HTTP 404'}
        result, _gm, post_mock = self._dispatch(bad)
        self.assertFalse(result['dispatched'])
        self.assertIn('workflow-dispatch guard', result['error'])
        post_mock.assert_not_called()
