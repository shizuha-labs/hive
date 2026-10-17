#!/usr/bin/env python3
"""Verify the preprovisioned replacement signing Secret without copying legacy values.

All credential bytes stay in process memory. The CI identity needs read-only
access to one Deployment and one Secret; it cannot create or change credentials.
"""
import base64
import json
import subprocess
import sys

NAMESPACE = 'rt-fleet'
DEPLOYMENT = 'shizuha-runtime-fleet'
SECRET = 'runtime-platform-jwt-v2'
KEY = 'jwt-secret'


class BootstrapError(RuntimeError):
    pass


def kubectl(*args, data=None):
    try:
        result = subprocess.run(['kubectl', '-n', NAMESPACE, *args], input=data,
                                text=True, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise BootstrapError('runtime JWT bootstrap Kubernetes request failed') from None
    if result.returncode:
        # Kubernetes errors may include submitted JSON. Do not forward them.
        raise BootstrapError('runtime JWT bootstrap Kubernetes request rejected')
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise BootstrapError('runtime JWT bootstrap received invalid Kubernetes response') from None


def validate_reference(deployment):
    entries = [entry for container in deployment.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
               for entry in container.get('env', []) if entry.get('name') == 'JWT_SECRET_KEY']
    if len(entries) != 1:
        raise BootstrapError('runtime Deployment must have exactly one JWT configuration')
    entry = entries[0]
    ref = entry.get('valueFrom', {}).get('secretKeyRef', {})
    if ('value' in entry or ref.get('name') != SECRET or ref.get('key') != KEY
            or ref.get('optional') is not False):
        raise BootstrapError('runtime Deployment must require the replacement signing Secret')


def ensure(call=kubectl):
    target = call('get', 'secret', SECRET, '-o', 'json')
    source = call('get', 'deployment', DEPLOYMENT, '-o', 'json')
    validate_reference(source)
    if target.get('immutable') is not True:
        raise BootstrapError('replacement signing Secret must already be immutable')
    try:
        value = base64.b64decode(target.get('data', {}).get(KEY, ''), validate=True)
    except (ValueError, TypeError):
        raise BootstrapError('replacement signing Secret encoding is invalid') from None
    if len(value) < 32 or not value.strip():
        raise BootstrapError('replacement signing Secret must contain at least 32 bytes')
    source_version = source.get('metadata', {}).get('resourceVersion')
    if not source_version or not target.get('metadata', {}).get('resourceVersion'):
        raise BootstrapError('runtime JWT verification requires Kubernetes resource versions')
    current = call('get', 'deployment', DEPLOYMENT, '-o', 'json')
    validate_reference(current)
    if current.get('metadata', {}).get('resourceVersion') != source_version:
        raise BootstrapError('runtime Deployment changed during verification')
    return 'immutable replacement signing Secret verified'


if __name__ == '__main__':
    try:
        print('Runtime JWT: ' + ensure())
    except BootstrapError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
