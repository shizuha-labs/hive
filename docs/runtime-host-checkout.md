# Legacy host-dist runtime configuration

The per-user daemon command still supports the legacy host-dist lane. Set
`HIVE_RUNTIME_HOST_CLI` to the reviewed absolute runtime checkout directory on
the selected host before invoking `provision_user_daemon --apply`. This is an
explicit deployment input; the host directory is mounted read-only at the stable
container path `/opt/shizuha-runtime`. Missing or ambiguous paths fail before
credential or cluster writes. The link-only credential flow needs no checkout.
Packaged runtime images and their release authority are unchanged.

For an existing installation, preserve its current approved host directory in
managed configuration before using the command after upgrade. Do not infer a
cluster host path from the API container's HOME. New installations can use a
service-owned directory such as `/srv/shizuha/runtime`.

The one-shot claw migration script likewise requires `S1_HOST`, `S1_DATA_DIR`
and `S1_WORKSPACE_DIR` to be supplied from the reviewed migration plan. Existing
application paths inside the OpenClaw image remain `/home/node/.openclaw`.

The advanced command also requires explicit `HIVE_RUNTIME_BASE_IMAGE`,
`HIVE_RUNTIME_BROKER_IMAGE`, `HIVE_RUNTIME_AGENT_NODE_SELECTOR`, and
`HIVE_RUNTIME_DAEMON_NODE_SELECTOR`. Selectors contain comma-separated unique
`key=value` node labels. The daemon placement must match the mounted host checkout;
agent placement is a separate reviewed installation decision. Image references
must point to reviewed artifacts published through the installation's CI.
All five inputs are validated before credential or cluster writes. Link-only
provisioning remains independent of this host-dist infrastructure. The portable
installer does not invoke this advanced command or infer private node identities.
