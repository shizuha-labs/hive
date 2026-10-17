#!/usr/bin/env bash
set -euo pipefail

# Deploy Hive backend with the production scheduling and registry invariants.
#
# Build/push may use an in-cluster registry address, but Kubernetes workloads
# must pull through the node-local insecure registry alias. The live Hive backend
# must also avoid s1 until its pod sandbox/container-start path is proven healthy.

NAMESPACE="${NAMESPACE:-shizuha-hive}"
DEPLOYMENT="${DEPLOYMENT:-hive}"
CONTAINER="${CONTAINER:-hive}"
PULL_REGISTRY="${PULL_REGISTRY:-localhost:30500}"
IMAGE_NAME="${IMAGE_NAME:-shizuha-hive}"
TAG="${TAG:?TAG is required, e.g. TAG=hive-abc123 ./scripts/deploy-hive-backend.sh}"
IMAGE="${IMAGE:-${PULL_REGISTRY}/${IMAGE_NAME}:${TAG}}"
STACK_DIR="${STACK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
REQUIRE_MULTIARCH="${REQUIRE_MULTIARCH:-${STACK_DIR}/deploy/k3s/scripts/require-multiarch-image.sh}"

require_multiarch_image() {
  local image="$1"
  if [[ -x "$REQUIRE_MULTIARCH" ]]; then
    "$REQUIRE_MULTIARCH" "$image"
    return
  fi

  if ! command -v docker >/dev/null 2>&1; then
    echo "Refusing deploy image ${image}: docker is required to verify image platforms" >&2
    exit 7
  fi
  if ! inspect="$(docker buildx imagetools inspect "$image" 2>/dev/null)"; then
    echo "Refusing deploy image ${image}: image manifest is not inspectable from this host" >&2
    exit 7
  fi
  for platform in linux/amd64 linux/arm64; do
    if ! grep -q "$platform" <<< "$inspect"; then
      echo "Refusing deploy image ${image}: missing required platform ${platform}" >&2
      echo "$inspect" >&2
      exit 7
    fi
  done
  echo "OK: ${image} includes required platforms: linux/amd64, linux/arm64"
}

case "$IMAGE" in
  localhost:30500/*|registry.local:30500/*) ;;
  *)
    echo "Refusing deploy image ${IMAGE}: workloads must use the node-local insecure registry alias" >&2
    exit 2
    ;;
esac

require_multiarch_image "$IMAGE"

previous_image="$(kubectl -n "$NAMESPACE" get "deploy/${DEPLOYMENT}" -o jsonpath="{.spec.template.spec.containers[?(@.name=='${CONTAINER}')].image}" 2>/dev/null || true)"
if [[ -n "$previous_image" ]]; then
  echo "Current image: ${previous_image}"
  echo "Rollback command if verification fails: kubectl -n ${NAMESPACE} set image deploy/${DEPLOYMENT} ${CONTAINER}=${previous_image} && kubectl -n ${NAMESPACE} rollout status deploy/${DEPLOYMENT}"
else
  echo "No current ${NAMESPACE}/${DEPLOYMENT} image found; rollback requires restoring the previous release manifest" >&2
fi

kubectl -n "$NAMESPACE" patch "deploy/${DEPLOYMENT}" --type=merge --patch-file=/dev/stdin <<'PATCH'
{
  "spec": {
    "strategy": {
      "type": "RollingUpdate",
      "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}
    },
    "template": {
      "spec": {
        "nodeSelector": null,
        "affinity": {
          "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
              "nodeSelectorTerms": [
                {
                  "matchExpressions": [
                    {"key": "shizuha.io/platform", "operator": "In", "values": ["true"]},
                    {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["s1"]}
                  ]
                }
              ]
            }
          }
        }
      }
    }
  }
}
PATCH

kubectl -n "$NAMESPACE" set image "deploy/${DEPLOYMENT}" "${CONTAINER}=${IMAGE}"
kubectl -n "$NAMESPACE" rollout status "deploy/${DEPLOYMENT}" --timeout="${ROLLOUT_TIMEOUT:-300s}"

live_image="$(kubectl -n "$NAMESPACE" get "deploy/${DEPLOYMENT}" -o jsonpath="{.spec.template.spec.containers[?(@.name=='${CONTAINER}')].image}")"
if [[ "$live_image" != "$IMAGE" ]]; then
  echo "Image mismatch: expected ${IMAGE}, got ${live_image}" >&2
  exit 3
fi

mapfile -t nodes < <(kubectl -n "$NAMESPACE" get pods -l app=hive -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.spec.nodeName}{"\n"}{end}')
if [[ "${#nodes[@]}" -eq 0 ]]; then
  echo "No running Hive pods found after rollout" >&2
  exit 4
fi
for node in "${nodes[@]}"; do
  if [[ "$node" == "s1" ]]; then
    echo "Hive backend landed on excluded node s1" >&2
    exit 5
  fi
done

bad_pods="$(kubectl -n "$NAMESPACE" get pods -l app=hive -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[0].state.waiting.reason}{"\n"}{end}' | grep -E 'ImagePull|ErrImage|ContainerCreating' || true)"
if [[ -n "$bad_pods" ]]; then
  echo "Hive rollout has unhealthy waiting pods:" >&2
  echo "$bad_pods" >&2
  exit 6
fi

echo "Hive backend deployed: ${IMAGE}"
echo "Nodes: ${nodes[*]}"
