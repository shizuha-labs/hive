# Optional installation profile in the Hive release ring

Hive's fleet ring preserves eight installation-owned values when the existing
`shizuha-hive/hive-installation-config` Secret is present. Keys equal the names:

- `HIVE_RUNTIME_HOST_CLI`
- `HIVE_RUNTIME_BASE_IMAGE`
- `HIVE_RUNTIME_BROKER_IMAGE`
- `HIVE_RUNTIME_AGENT_NODE_SELECTOR`
- `HIVE_RUNTIME_DAEMON_NODE_SELECTOR`
- `HIVE_HOSTPLANE_SSH_TAILNET_CIDRS`
- `HIVE_HOSTPLANE_SSH_BOOTSTRAP_CIDRS`
- `SHIZUHA_PULSE_HUMAN_EMAILS`

The helper renders Secret identity metadata and key names only; it never outputs
Secret values. All eight required names must exist before any Deployment read or patch.
An absent profile leaves all existing/fresh-install environment unchanged.
When present, the ring checks both exact Deployment/container owners (`hive` and
`hive-api`) before patching. A strategic merge by container and environment name
replaces only these values with required SecretKeyRefs. It explicitly clears old
literal values and replaces competing valueFrom sources. Other variables,
containers and envFrom entries are preserved. No Secret is created by this path.

Existing installations must populate the reviewed current effective values
privately before source activation. Prefer existing explicit settings over
historical defaults during that audit. Do not deploy example private data.
Runtime human routing defaults are projected only when an agent has no explicit
value; an explicit empty per-agent value remains authoritative.

`fleet.installation_config.effective_agent_env` makes a fresh copy at native
container render, daemon projection/provision and per-user roster output. Daemon
reconnect, partial edits and durable retries use the same inheritance rule.
Unrelated partial edits do not add an environment replacement. Hive stores only
explicit per-agent environment edits; installation values are not copied into
`FleetAgent.env`, pending config or lane outboxes. Transport fingerprints bind
the effective outgoing edit, and confirmation is compared against the current
effective value so an older installation value cannot clear a newer pending edit.
Changing installation configuration takes effect through the ordinary release
and desired-state reconciliation paths, without a fleet database rewrite.
