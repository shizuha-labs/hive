FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

# The immutable application image also hosts the bounded ring executor. Keep
# its diagnostics/health probes self-contained rather than depending on tools
# installed on an ephemeral Origin runner.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends ca-certificates curl wget iproute2 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install dependencies from the committed lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-editable

# Copy application
ARG CACHE_BUST=1
COPY . .

# The R1/R2 release executor runs in-cluster with a dedicated ServiceAccount.
# CI places the architecture-matched, checksum-verified client in the build
# context; the release runner never mounts or copies its broad kubeconfig into
# the executor.
# The Origin package is the k3s multicall binary. It selects kubectl from
# argv[0], so its installed filename must remain exactly "kubectl".
COPY kubectl /usr/local/bin/kubectl
RUN chmod 0755 /usr/local/bin/kubectl \
    && chmod 0755 /app/.forgejo/scripts/kubectl \
                  /app/.forgejo/scripts/ring-executor.py \
                  /app/.forgejo/scripts/ring_executor.py \
                  /app/.forgejo/scripts/ring-recovery.py \
                  /app/.forgejo/scripts/ring-cleanup-canary.sh \
                  /app/.forgejo/scripts/ring-release-canary.sh \
                  /app/.forgejo/scripts/ring-release-fleet.sh

# Collect static files
RUN python manage.py collectstatic --noinput 2>/dev/null || true

EXPOSE 8030 8031

# HIVE-235/SCLI-199: serve HTTP + WebSocket on the same port so fleet daemons can
# dial /v1/fleet/daemon-link (DaemonLinkConsumer). gunicorn/gthread is WSGI-only and
# 404s the WS path; daphne (already a dep) is the ASGI server channels expects.
# Concurrency: async event loop handles concurrent IO-bound control-plane traffic;
# scale out with Deployment replicas (channel layer = Redis DB 5), not gthread workers.
# -t 120 matches the previous gunicorn timeout for slow Pulse/k8s upstreams.
# HIVE-725: one Daphne process serves two Twisted endpoints so the ASGI scope's
# local server port is a transport-derived (not header-derived) routing input.
# Port 8031 is reachable only through the dedicated Connect Service/NetPol.
CMD ["daphne", "-e", "tcp:port=8030:interface=0.0.0.0", "-e", "tcp:port=8031:interface=0.0.0.0", "-t", "120", "--proxy-headers", "hive_project.asgi:application"]
