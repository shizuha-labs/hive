# PLAT-4086 R1 clean source-controlled canary. It is additive and addressable
# only through hive-canary; the stable Service never selects these pods.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hive-canary
  namespace: shizuha-hive
  labels:
    app: hive-canary
    app.kubernetes.io/name: hive
    release_ring: canary
    build_sha: __BUILD_SHA__
  annotations: {shizuha.io/task: PLAT-4086, shizuha.io/ring: canary}
spec:
  replicas: 1
  selector: {matchLabels: {app: hive-canary, release_ring: canary}}
  template:
    metadata:
      labels:
        app: hive-canary
        app.kubernetes.io/name: hive
        release_ring: canary
        build_sha: __BUILD_SHA__
    spec:
      serviceAccountName: hive-controller
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - {key: shizuha.io/platform, operator: In, values: ["true"]}
                  - {key: kubernetes.io/hostname, operator: NotIn, values: ["s1", "i9-ws"]}
      tolerations:
        - {key: node.shizuha/workstation, operator: Equal, value: "true", effect: NoSchedule}
      containers:
        - name: hive
          image: __IMAGE__
          imagePullPolicy: Always
          ports: [{name: http, containerPort: 8030}]
          readinessProbe:
            httpGet: {path: /api/health/, port: http}
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 2
          # PLAT-4210: restart a truly-hung ASGI worker (see hive-backend-live.yaml).
          livenessProbe:
            httpGet: {path: /api/health/, port: http}
            initialDelaySeconds: 40
            periodSeconds: 15
            timeoutSeconds: 10
            failureThreshold: 6
          env:
            - name: HIVE_TOKEN_COORDINATOR_KEK
              valueFrom: {secretKeyRef: {name: hive-secrets, key: HIVE_TOKEN_COORDINATOR_KEK}}
            - name: HIVE_TOKEN_COORDINATOR_KEK_ID
              valueFrom: {secretKeyRef: {name: hive-secrets, key: HIVE_TOKEN_COORDINATOR_KEK_ID}}
            - name: HIVE_ID_FLEET_PROVISIONER_TOKEN
              valueFrom: {secretKeyRef: {name: fleet-provisioner-token, key: token}}
            - {name: DB_NAME, value: shizuha_hive}
            - {name: DB_USER, value: postgres}
            - name: DB_PASSWORD
              valueFrom: {secretKeyRef: {name: shizuha-infra-postgresql, key: postgres-password}}
            - {name: DB_HOST, value: shizuha-infra-postgresql.shizuha.svc.cluster.local}
            - {name: DB_PORT, value: "5432"}
            - name: JWT_SECRET_KEY
              valueFrom: {secretKeyRef: {name: hive-secrets, key: jwt-secret}}
            - name: SHIZUHA_JWT_SIGNING_KEY
              valueFrom: {secretKeyRef: {name: hive-secrets, key: jwt-secret}}
            - {name: SHIZUHA_PLATFORM_URL, value: http://shizuha-nginx.shizuha.svc.cluster.local}
            - {name: SHIZUHA_CONNECT_INTERNAL_URL, value: http://shizuha-connect.shizuha.svc.cluster.local:8019}
            - {name: HIVE_PUBLIC_PLATFORM_URL, value: https://shizuha.com}
            - {name: DEBUG, value: "False"}
            # Source-only native27 ConfigMap must be bootstrapped before this consumer activates.
            - {name: HIVE_NATIVE_RUNTIME_RELEASE_ENABLED, value: "true"}
            - {name: HIVE_NATIVE_RUNTIME_RELEASE_NAMESPACE, value: shizuha-hive}
            - {name: VLLM_BASE_URL, value: http://cortex.shizuha-cortex.svc.cluster.local:8040}
            - {name: CORTEX_PUBLIC_BASE_URL, value: http://cortex.shizuha-cortex.svc.cluster.local:8040/v1}
            - name: CORTEX_API_KEY
              valueFrom: {secretKeyRef: {name: hive-secrets, key: CORTEX_API_KEY}}
            - {name: REDIS_URL, value: "redis://shizuha-redis-ha-master.shizuha.svc.cluster.local:6379/5"}
            - {name: CACHE_REDIS_URL, value: "redis://shizuha-redis-ha-master.shizuha.svc.cluster.local:6379/6"}
            - {name: AGENT_HEARTBEAT_REDIS_URL, value: "redis://shizuha-redis-ha-master.shizuha.svc.cluster.local:6379/1"}
            - {name: RUNTIME_FLEET_API_URL, value: "http://shizuha-runtime-fleet.rt-fleet.svc.cluster.local:8016"}
            - {name: FLEET_COORDINATOR_MODEL_TOKEN_URL, value: "http://hive-direct.shizuha-hive.svc.cluster.local:8030/hive/api/v1/coordinator/model-token"}
            - name: HIVE_INTERNAL_AUTH_TOKEN
              valueFrom: {secretKeyRef: {name: hive-secrets, key: internal-auth-token}}
            - name: COMPANY_STATE_READ_TOKEN
              valueFrom: {secretKeyRef: {name: hive-company-state-read, key: token, optional: false}}
            - name: HIVE_PULSE_OAUTH_CLIENT_ID
              valueFrom: {secretKeyRef: {name: hive-pulse-fleet-read, key: client-id, optional: false}}
            - name: HIVE_PULSE_OAUTH_CLIENT_SECRET
              valueFrom: {secretKeyRef: {name: hive-pulse-fleet-read, key: client-secret, optional: false}}
            - name: HIVE_PULSE_OAUTH_TOKEN_URL
              valueFrom: {secretKeyRef: {name: hive-pulse-fleet-read, key: token-url, optional: false}}
            # Exercise the same signed Admin membership producer as the fleet.
            - name: HIVE_SERVICE_SIGNING_KEY
              valueFrom: {secretKeyRef: {name: hive-service-signing-key, key: key, optional: true}}
            - name: HIVE_CAPABILITY_EVIDENCE_AUTH_TOKEN
              valueFrom: {secretKeyRef: {name: hive-capability-evidence-client-auth, key: token}}
            - name: HIVE_CAPABILITY_EVIDENCE_PRIVATE_KEY_PEM
              valueFrom: {secretKeyRef: {name: hive-capability-evidence-signer, key: private-key.pem, optional: true}}
            - name: HIVE_CAPABILITY_EVIDENCE_KID
              valueFrom: {secretKeyRef: {name: hive-capability-evidence-signer, key: kid, optional: true}}
            - name: HIVE_CAPABILITY_EVIDENCE_SERVICE_ACCOUNT
              value: hive-controller
            - name: RUNTIME_FLEET_CONTROL_SECRET
              valueFrom: {secretKeyRef: {name: runtime-fleet-control-secret, key: secret}}
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {cpu: "2", memory: 2Gi}
---
apiVersion: v1
kind: Service
metadata:
  name: hive-canary
  namespace: shizuha-hive
  labels: {service: hive, release_ring: canary, build_sha: __BUILD_SHA__}
spec:
  selector: {app: hive-canary, release_ring: canary, build_sha: __BUILD_SHA__}
  ports: [{name: http, port: 8030, targetPort: http}]
---
apiVersion: monitoring.coreos.com/v1
kind: Probe
metadata:
  name: probe-shizuha-hive-canary-__BUILD_SHORT__
  namespace: monitoring
  labels:
    release: kube-prometheus
    app.kubernetes.io/component: synthetic-probes
    service: hive
    release_ring: canary
    build_sha: __BUILD_SHA__
    shizuha.io/task: PLAT-4086
spec:
  jobName: shizuha-hive-canary-__BUILD_SHORT__
  prober:
    url: blackbox-exporter.monitoring.svc.cluster.local:9115
    scheme: http
    path: /probe
  module: http_2xx_insecure
  interval: 30s
  scrapeTimeout: 15s
  targets:
    staticConfig:
      static: [http://hive-canary.shizuha-hive.svc.cluster.local:8030/api/health/]
      labels:
        service: hive
        release_ring: canary
        build_sha: __BUILD_SHA__
        team: engineering
