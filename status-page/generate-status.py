#!/usr/bin/env python3
"""
Status page generator — reads Prometheus targets and generates status.json.

Runs as a sidecar next to nginx (see Dockerfile.generator: generate → sleep 300
loop). Outputs to STATUS_DIR (default: /data), which is the shared emptyDir
nginx serves as /live/ (HIVE-333 P1-2).
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
STATUS_DIR = os.environ.get("STATUS_DIR", "/data")

# Map Prometheus job names to status page component names
COMPONENT_MAP = {
    "shizuha-id": "Shizuha ID (Auth)",
    "shizuha-pulse": "Shizuha Pulse (Tasks)",
    "shizuha-wiki": "Shizuha Wiki (Docs)",
    "shizuha-connect": "Shizuha Connect (Social)",
    "shizuha-drive": "Shizuha Drive (Storage)",
    "shizuha-notes": "Shizuha Notes",
    "shizuha-mail": "Shizuha Mail",
    "shizuha-admin": "Shizuha Admin",
    "cortex": "Cortex (Inference)",
    "agent-runtime": "Agent Runtime",
}

# Components not yet in Prometheus — shown as operational by default
STATIC_COMPONENTS = [
    {"name": "Shizuha Books", "status": "maintenance", "label": "Offline"},
    {"name": "Shizuha HR", "status": "maintenance", "label": "Offline"},
    {"name": "Shizuha Time", "status": "maintenance", "label": "Offline"},
]


def query_prometheus(query: str) -> list:
    """Query Prometheus instant vector and return results."""
    url = f"{PROMETHEUS_URL}/api/v1/query?query={query}"
    try:
        req = Request(url)
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data["status"] != "success":
            print(f"Prometheus query failed: {data}", file=sys.stderr)
            return []
        return data["data"]["result"]
    except (URLError, json.JSONDecodeError, KeyError) as e:
        print(f"Prometheus query error: {e}", file=sys.stderr)
        return []


def get_component_status() -> list:
    """Get component health from Prometheus up metric."""
    results = query_prometheus('up')
    if not results:
        # Fallback: all unknown
        components = []
        for name in COMPONENT_MAP.values():
            components.append({
                "name": name,
                "status": "degraded",
                "label": "Unknown",
            })
        components.extend(STATIC_COMPONENTS)
        return components

    # Build status from up metric
    up_map = {}
    for r in results:
        job = r["metric"].get("job", "")
        value = int(r["value"][1])
        up_map[job] = value

    components = []
    for job, name in COMPONENT_MAP.items():
        if job in up_map:
            if up_map[job] == 1:
                components.append({"name": name, "status": "operational", "label": "Operational"})
            else:
                components.append({"name": name, "status": "outage", "label": "Down"})
        else:
            components.append({"name": name, "status": "degraded", "label": "Unknown"})

    components.extend(STATIC_COMPONENTS)
    return components


def compute_overall(components: list) -> str:
    """Compute overall status from components."""
    statuses = {c["status"] for c in components}
    if "outage" in statuses:
        return "outage"
    if "degraded" in statuses:
        return "degraded"
    return "operational"


def load_incidents() -> list:
    """Load incidents from a local JSON file (manually maintained)."""
    incidents_file = os.path.join(STATUS_DIR, "incidents.json")
    try:
        with open(incidents_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def load_uptime() -> dict:
    """Load uptime data from a local JSON file.

    HIVE-333 P2: until uptime is actually computed from Prometheus history, the
    fallback must say so — a public page must not assert unmeasured uptime.
    """
    uptime_file = os.path.join(STATUS_DIR, "uptime.json")
    try:
        with open(uptime_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"org": "not yet measured", "team": "not yet measured", "assistant": "N/A"}


def main():
    components = get_component_status()
    overall = compute_overall(components)
    incidents = load_incidents()
    uptime = load_uptime()

    now = datetime.now(timezone.utc).astimezone()
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now_ist = now.astimezone(ist_offset)
    updated_at = now_ist.strftime("%Y-%m-%dT%H:%M:%S+05:30")

    status = {
        "overall": overall,
        "updated_at": updated_at,
        "components": components,
        "incidents": incidents,
        "uptime": uptime,
    }

    # Atomic replace: nginx serves this file live from the shared emptyDir, so
    # a partially-written status.json must never be observable.
    output_path = os.path.join(STATUS_DIR, "status.json")
    tmp_path = output_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp_path, output_path)
    print(f"Status page generated: {output_path}")
    print(f"  Overall: {overall}")
    print(f"  Components: {len(components)}")
    print(f"  Incidents: {len(incidents)}")


if __name__ == "__main__":
    main()
