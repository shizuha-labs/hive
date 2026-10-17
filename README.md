# Shizuha Hive

Agent configuration, runtime lifecycle, credentials and fleet coordination for the Shizuha platform.

## Deploy the platform

Use the [platform deployment repository](https://github.com/shizuha-labs/deploy)
for the coordinated installation of ID, Hive, Pulse, Connect, Wiki and Atlas.
Service repositories provide application source; deployment configuration owns
hostnames, credentials, storage and inter-service trust.

Django API and React frontend; PostgreSQL, Redis and an explicitly configured agent runtime. Kubernetes-backed provisioning additionally requires a configured cluster and service identity.

Configuration is read by [`hive_project/settings.py`](hive_project/settings.py). Supply deployment-specific
credentials and endpoints through the environment. Existing production-cluster
manifests and internal CI workflows are not an external installation recipe.

## Development

Backend dependencies are declared in `pyproject.toml` and `uv.lock`;
frontend dependencies are in `frontend/package.json` and its lockfile.
The repository's `.forgejo/workflows/premerge.yml` documents its test recipe.
Run tests against isolated databases and service dependencies.

## License

Original code is available under the [MIT License](LICENSE). Third-party
components retain their respective licenses and notices. Dependency licenses
are not replaced by this repository's license.

Per-user Kubernetes daemons use a required `runtime-platform-jwt` Secret in the
daemon namespace, key `jwt-secret`. Deployment overrides may select another
Secret using `USER_DAEMON_JWT_SECRET_NAME` and `USER_DAEMON_JWT_SECRET_KEY`.
There is no source credential or plaintext fallback. The first-party Origin
bootstrap projects the existing runtime signing value into a precreated named
Secret once, makes it immutable, and refuses mismatches instead of rotating it.
