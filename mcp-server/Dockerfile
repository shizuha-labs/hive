# Shizuha Hive MCP Server (HIVE-MCP-1) — mirrors the pulse MCP server image.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "from mcp.server.fastmcp import FastMCP; from mcp.server.sse import TransportSecuritySettings"

# Copy the server, shared base utilities, and release-blocking OAuth regression.
COPY base.py hive_server.py test_oauth_defaults.py ./

# Streamable-HTTP port for the Hive MCP server.
EXPOSE 18107

# Run as a persistent streamable-HTTP daemon. Config is supplied via env
# (SHIZUHA_HIVE_URL / SHIZUHA_BASE_URL, optional SHIZUHA_HIVE_JWT_TOKEN) or the
# per-request Authorization header.
CMD ["python", "hive_server.py", "--http-port", "18107"]
