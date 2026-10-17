import json
import re
import sys
from pathlib import Path


AUTH_ENVIRONMENT = {
    'SHIZUHA_MCP_OAUTH_ENFORCE',
    'SHIZUHA_MCP_RESOURCE_URL',
    'SHIZUHA_OAUTH_ISSUER',
    'SHIZUHA_PUBLIC_HOST',
    'SHIZUHA_PUBLIC_SCHEME',
}


def read_manifest_json(path):
    payload = Path(path).read_text().strip()
    decoder = json.JSONDecoder()
    resources = []
    while payload:
        resource, end = decoder.raw_decode(payload)
        resources.append(resource)
        payload = payload[end:].lstrip()
    if not resources:
        raise ValueError('canonical deployment manifest is empty')
    return resources[0] if len(resources) == 1 else {'kind': 'List', 'items': resources}


def deployment_contract(manifest):
    resources = manifest.get('items', []) if manifest.get('kind') == 'List' else [manifest]
    matches = [resource for resource in resources if (
        resource.get('kind') == 'Deployment'
        and resource.get('metadata', {}).get('name') == 'shizuha-hive-mcp'
        and resource.get('metadata', {}).get('namespace') == 'shizuha'
    )]
    if len(matches) != 1:
        raise ValueError('expected one canonical shizuha/shizuha-hive-mcp Deployment')
    containers = matches[0]['spec']['template']['spec']['containers']
    matches = [container for container in containers if container.get('name') == 'hive-mcp']
    if len(matches) != 1:
        raise ValueError('expected one declared hive-mcp container')
    environment = [entry for entry in matches[0].get('env', []) if entry.get('name') in AUTH_ENVIRONMENT]
    values = {entry['name']: entry.get('value') for entry in environment}
    if len(values) != len(environment) or any('valueFrom' in entry for entry in environment):
        raise ValueError('authentication contract must contain unique explicit values')
    required = {
        'SHIZUHA_MCP_OAUTH_ENFORCE': 'true',
        'SHIZUHA_MCP_RESOURCE_URL': 'https://shizuha.com/mcp/hive',
        'SHIZUHA_OAUTH_ISSUER': 'https://shizuha.com/id',
        'SHIZUHA_PUBLIC_HOST': 'shizuha.com',
        'SHIZUHA_PUBLIC_SCHEME': 'https',
    }
    if any(values.get(name) != value for name, value in required.items()):
        raise ValueError('canonical Hive MCP bearer enforcement contract is missing or unsafe')
    return environment


def render_patch(manifest, current, image):
    if not re.fullmatch(r'localhost:30500/shizuha-hive-mcp:mcp-[0-9a-f]{7,40}', image):
        raise ValueError('candidate must be a CI-produced Hive MCP image reference')
    metadata = current.get('metadata', {})
    if (
        current.get('kind') != 'Deployment'
        or metadata.get('name') != 'shizuha-hive-mcp'
        or metadata.get('namespace') != 'shizuha'
        or not metadata.get('uid')
        or not metadata.get('resourceVersion')
    ):
        raise ValueError('live Deployment identity and resourceVersion are required')
    containers = current['spec']['template']['spec']['containers']
    indexes = [index for index, container in enumerate(containers) if container.get('name') == 'hive-mcp']
    if len(indexes) != 1:
        raise ValueError('expected one live hive-mcp container')
    index = indexes[0]
    desired = deployment_contract(manifest)
    retained = [entry for entry in containers[index].get('env', []) if entry.get('name') not in AUTH_ENVIRONMENT]
    return [
        {'op': 'test', 'path': '/metadata/uid', 'value': metadata['uid']},
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': metadata['resourceVersion']},
        {'op': 'replace', 'path': f'/spec/template/spec/containers/{index}/image', 'value': image},
        {'op': 'add', 'path': f'/spec/template/spec/containers/{index}/env', 'value': retained + desired},
    ]


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('usage: render-hive-mcp-deployment.py declared.json current.json image')
    patch = render_patch(
        read_manifest_json(sys.argv[1]),
        json.loads(Path(sys.argv[2]).read_text()),
        sys.argv[3],
    )
    print(json.dumps(patch, separators=(',', ':')))
