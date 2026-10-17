"""Dispatch-intake YAML guard (PLAT-9683 leg 4, dispatch-side pre-validation).

The PLAT-9660 class: a workflow file that fatalizes the runner's YAML decoder
(e.g. a duplicate bare ``workflow_dispatch`` mapping key) dies pre-log at
dispatch intake — Forgejo returns 204-but-no-run and the runner restarts
silently, indistinguishable from infra vanish. Fleet-automation dispatch
callers see ``dispatched: True`` while nothing ever runs.

This module kills that class for Hive's own dispatches by validating the
target workflow file AT THE DISPATCH REF with a duplicate-key-rejecting
safe-load before the dispatch POST is fired. Any strict-decode failure (the
Go ``go.yaml.in/yaml/v4`` decoder fatal class the runner uses at intake,
including duplicate mapping keys anywhere in the document) blocks the
dispatch loudly with the decoder-style parse error (key + line + column).

Fail-closed by design: a workflow we cannot fetch or cannot strictly decode
is exactly the silent-204 surface. A transient raw-API failure blocks one
dispatch tick (self-healing on the next tick); a silent 204 is the defect.

Env kill-switch: ``HIVE_WORKFLOW_DISPATCH_GUARD=0`` disables the guard
(emergency use only — restores the pre-guard dispatch behavior).
"""
from __future__ import annotations

import logging
import os

import requests
import yaml

logger = logging.getLogger(__name__)

DEFAULT_WORKFLOW_DIR = '.forgejo/workflows'
_RAW_TIMEOUT = 10
_GUARD_ENV = 'HIVE_WORKFLOW_DISPATCH_GUARD'


class StrictWorkflowLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that rejects duplicate mapping keys anywhere.

    Mirrors the intake decoder's fatal: Go's ``go.yaml.in/yaml/v4`` refuses
    duplicate mapping keys; PyYAML's stock ``safe_load`` silently keeps the
    last one, which would let a duplicate-key workflow slip through a plain
    validation. The raised ``ConstructorError`` carries the mark of the
    duplicate key node (line/column), i.e. the decoder's exact parse error.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                hash(key)
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    'while constructing a mapping',
                    node.start_mark,
                    f'found unhashable key {key!r}',
                    key_node.start_mark,
                ) from exc
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    'while constructing a mapping',
                    node.start_mark,
                    f'found duplicate key {key!r}',
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _guard_enabled() -> bool:
    return os.environ.get(_GUARD_ENV, '1').strip().lower() not in ('0', 'false', 'no')


def fetch_workflow_source(repo: str, ref: str, workflow_path: str, *, api: str, token: str) -> str:
    """Read the workflow file's raw content at ``ref`` (raises on failure)."""
    url = f'{api}/api/v1/repos/{repo}/raw/{workflow_path}?ref={ref}'
    response = requests.get(
        url,
        headers={'Authorization': f'token {token}'},
        timeout=_RAW_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f'workflow file not readable at dispatch ref: '
            f'GET {url} -> HTTP {response.status_code}: {response.text[:200]}'
        )
    return response.text


def validate_workflow_yaml(source: str) -> dict:
    """Strict-load a workflow YAML source; ``{'ok': True}`` or named failure.

    Returns ``{'ok': False, 'error': <decoder-style parse error>}`` for any
    YAML decode failure (duplicate keys, malformed syntax, non-mapping doc)
    — the classes that fatalize the runner's intake decode pre-log.
    """
    try:
        doc = yaml.load(source, Loader=StrictWorkflowLoader)
    except yaml.YAMLError as exc:
        return {'ok': False, 'error': f'workflow YAML strict-decode failed: {exc}'}
    if not isinstance(doc, dict):
        return {
            'ok': False,
            'error': (
                f'workflow YAML strict-decode failed: top-level document is '
                f'{type(doc).__name__}, expected a mapping (runner intake '
                f'decode would fatal pre-log)'
            ),
        }
    return {'ok': True}


def validate_workflow_dispatch_yaml(
    repo: str,
    ref: str,
    workflow_filename: str,
    *,
    api: str,
    token: str,
    workflow_dir: str = DEFAULT_WORKFLOW_DIR,
) -> dict:
    """Validate the target workflow file at the dispatch ref before firing.

    Contract: ``{'ok': True}`` when the file is readable and strict-decodes;
    ``{'ok': False, 'error': <named reason>}`` otherwise (fetch failure or
    strict-decode failure). Never raises.
    """
    if not _guard_enabled():
        return {'ok': True, 'skipped': 'guard disabled via ' + _GUARD_ENV}
    workflow_path = f'{workflow_dir}/{workflow_filename}'
    try:
        source = fetch_workflow_source(repo, ref, workflow_path, api=api, token=token)
    except Exception as exc:  # noqa: BLE001 — fail closed with a named reason
        return {'ok': False, 'error': f'workflow-dispatch guard could not read {workflow_path} at {ref}: {exc}'}
    result = validate_workflow_yaml(source)
    if not result['ok']:
        result['error'] = (
            f'workflow-dispatch guard: {workflow_path} at {ref}: {result["error"]}'
        )
    return result
