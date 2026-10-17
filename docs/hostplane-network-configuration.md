# Installation-owned hostplane SSH egress

Fresh installations do not infer host targets or permit SSH to an operator's
network. The isolated-agent policy producer reads explicit JSON arrays from
`HIVE_HOSTPLANE_SSH_TAILNET_CIDRS` and `HIVE_HOSTPLANE_SSH_BOOTSTRAP_CIDRS`.
Use reviewed, canonical, unique CIDRs for the exact intended hosts/networks.
Unspecified lists render `egress: []`, a deny policy; an empty allow peer rule is
never generated. The selector remains the existing DevOps SSH role and the only
allowed port is TCP22. Generic default-deny, DNS/platform and LLM policies remain.

Before upgrading an existing installation, preserve its reviewed effective lists
in managed private deployment configuration. Do not expand access during this
migration. Invalid configuration stops before policy API writes. Temporary LAN
bootstrap is separate and can be retired without removing tailnet access.
