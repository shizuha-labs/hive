# Hive-native runtime release authority

This scope supersedes the daemon release ADR only for current FleetAgent rows whose actual Kubernetes Deployment is owned by `shizuha-hive` and matches the row's namespace, name and username. A daemon-linked row can meet that contract. Legacy daemon-owned Claude/Codex and their desired lifecycle, roster, image and controller authority are unchanged.

The source-controlled `shizuha-hive/hive-native-runtime-release` ConfigMap contains `desired.json`, scope `hive-native`, and an append-only release history. Its first record is the explicit generation27/e5 bootstrap. The annotation `shizuha.io/native-runtime-document-sha256` binds the raw document and `shizuha.io/native-runtime-source-commit` identifies its full source revision. Native releases use immutable OCI index digests and the existing approval/source/intent fields. Rollbacks append a higher generation referencing an earlier native record; they never lower the generation.

## Activation and publication

1. Bootstrap the reviewed native27 document before deploying the consumer. Exact e5 Deployments can be attributed to that source without changing their templates or restarting their Pods.
2. Deploy the Hive consumer through its ordinary canary/fleet CI. The canary template and shared fleet renderer set `HIVE_NATIVE_RUNTIME_RELEASE_ENABLED=true` and the authority namespace `shizuha-hive`. Generated workers and CronJobs inherit those values from the post-rollout Hive container. Existing `hive-controller` read authority covers this named ConfigMap; no new fleet write authority is introduced.
3. Read `manage.py native_runtime_release_snapshot` through the existing CI exec/read authority. Before native28 publication, verify every production Hive Deployment/CronJob template, nonterminal Job template, active ReplicaSet and nonterminal Pod has the reviewed consumer image and native activation. An old Job with no Pod remains an old writer. The current API image alone does not establish this barrier. `--bootstrap` permits reading explicit native27 before activation; it never writes or activates anything.
4. Publish later reviewed records through the native source publication lane. The existing on-demand/start/config lifecycle consumes them. The native API `/api/v1/fleet/native-runtime-image` is read-only; POST does not publish releases. The legacy `/runtime-image` remains an independent daemon projection.

After activation, absent, malformed, conflicting or unreadable native authority refuses actuation. There is no daemon/static/row-image fallback. Publishing a document is desired state, not proof that a Pod upgraded.

## Live boundary

### Harness-drift controller release ownership

`fleet-harness-drift-roll` is owned by Hive's fenced fleet release, not by the
historical hand-pinned deploy manifest. The renderer takes the qualified Hive
container's image, database/native-authority environment, secret projections,
mounts, resources and pod security settings. It sets the management command
explicitly to `roll_harness_drift --max-seats 6`, clears web-server arguments and
probes, and verifies the applied template. Its existing fifteen-minute schedule
and `Forbid` policy are unchanged; finished Job evidence is retained for an hour.
Catch-up is bounded to one existing schedule interval (`startingDeadlineSeconds:
900`). After the old web-server Job occupied the slot for over 100 missed ticks,
Kubernetes reported `TooManyMissedTimes`. This periodic reconciler needs the
current interval, not replay of obsolete intervals. The bounded catch-up window
allows normal scheduling to resume without changing reconciliation frequency
or terminating active work. See the [Kubernetes CronJob scheduling contract](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/#job-creation).

Do not image-patch a CronJob by replacing its container list. That can remove its
command and environment, launch the image's default web server indefinitely and
block subsequent runs under `Forbid`. Updating a CronJob does not repair an
already-created Job. Preserve its evidence and use the established incident
procedure to retire a proven stale writer separately; never treat a template
update as completion of the native writer census.

The exact CronJob is part of ring fencing and takeover inventory. Bootstrap of
its previously unfenced legacy object requires its exact namespace/name,
platform label, controller service account and single Hive `roll` container.
Foreign generation/token ownership remains rejected by the existing CAS fence.
Recovery must have its exact existing-object read/patch capability before this
target is promoted; adding a renderer without recoverable ownership is not a
complete release.

An active template replacement requires the original desired row generation/config/executor/operation, release document identity/hash, exact Deployment UID/resourceVersion, current model lease, and one Ready Pod owned through the Deployment's ReplicaSet. It arms one existing60s runtime drain, then uses GET observations within that same lease. It does not extend the drain or change controller schedules. Busy, unknown, protocol1, missing ingress fencing, extra/Pending/terminating Pods, process restart, source change or lost lease defers the action.

The final proof rereads the exact protocol2 nonce, target, admission version and lease deadline after other blocking reads, checks the same Pod UID/containerID, then bounds the Kubernetes CAS request by the remaining reservation. Conflicts re-read and reacquire proof. A transport-ambiguous write is not evidence that the old process terminated; publication separately checks owned Ready replacement Pods and their actual image IDs against the qualified OCI index/children.

Dormant older or unknown templates are reported as unadopted/deferred, retained during publication, and never counted as upgraded. A direct release-convergence call leaves an unknown dormant template unchanged. Existing stop/config staging of already adopted templates is retained. The next existing authorized start may reconstruct the full template from the current qualified target, after freshly proving native ownership, zero replicas, no Pod, original desired intent and start lease. It then rechecks source/intent/lease/UID/resourceVersion before scale. No old process executes or gets upgraded in this cold path. Active unknown images and deleted/unowned remnants remain quarantined; release publication does not revive or retire them.

## Verification

`fleet.test_native_runtime_release` drives real provisioner/start/scale and API callers with real ORM state and Kubernetes wire objects. It covers same-image bootstrap, busy-to-ready continuation, missing/stale authority, conflicts, nonce/process changes, finite drain expiry, no-Pod races, original-operation preservation, cold materialization, lease loss, higher-generation rollback, legacy exclusion and delayed old writer Jobs. Run it through the project's `manage.py test` recipe, then run the full `.ci-local-gate.sh` including ring and frontend checks before publishing source.
