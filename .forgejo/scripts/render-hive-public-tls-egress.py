#!/usr/bin/env python3
"""Render the one canonical existing Hive public TLS connectivity policy."""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def render():
    documents = [doc for doc in yaml.safe_load_all(
        (ROOT / 'k8s/shizuha-hive-netpol.yaml').read_text()) if doc]
    matches = [doc for doc in documents if doc.get('kind') == 'NetworkPolicy'
               and doc['metadata'].get('namespace') == 'shizuha-hive'
               and doc['metadata'].get('name') == 'allow-platform-egress']
    if len(matches) != 1:
        raise ValueError('exactly one canonical Hive platform egress policy is required')
    return matches[0]


if __name__ == '__main__':
    print(json.dumps(render()))
