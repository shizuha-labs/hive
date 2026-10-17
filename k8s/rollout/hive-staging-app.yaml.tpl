# PLAT-4086 R0 app and build-attributed synthetic probe.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hive
  namespace: staging
  labels:
    app: hive
    app.kubernetes.io/name: hive
    release_ring: staging
    build_sha: __BUILD_SHA__
  annotations: {shizuha.io/task: PLAT-4086}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app: hive, release_ring: staging}}
  template:
    metadata:
      labels:
        app: hive
        app.kubernetes.io/name: hive
        release_ring: staging
        build_sha: __BUILD_SHA__
    spec:
      containers:
        - name: hive
          image: __IMAGE__
          imagePullPolicy: Always
          ports: [{name: http, containerPort: 8030}]
          readinessProbe:
            httpGet: {path: /api/health/, port: http}
            initialDelaySeconds: 5
            periodSeconds: 5
            timeoutSeconds: 2
          # PLAT-4210: restart a truly-hung ASGI worker (see hive-backend-live.yaml).
          livenessProbe:
            httpGet: {path: /api/health/, port: http}
            initialDelaySeconds: 40
            periodSeconds: 15
            timeoutSeconds: 10
            failureThreshold: 6
          env:
            - {name: DB_NAME, value: shizuha_hive_staging}
            - {name: DB_USER, value: postgres}
            - {name: DB_PASSWORD, value: staging-only}
            - {name: DB_HOST, value: hive-staging-postgres.staging.svc.cluster.local}
            - {name: DB_PORT, value: "5432"}
            - {name: REDIS_URL, value: "redis://hive-staging-redis.staging.svc.cluster.local:6379/5"}
            - {name: CACHE_REDIS_URL, value: "redis://hive-staging-redis.staging.svc.cluster.local:6379/6"}
            - {name: AGENT_HEARTBEAT_REDIS_URL, value: "redis://hive-staging-redis.staging.svc.cluster.local:6379/1"}
            - {name: JWT_SECRET_KEY, value: staging-only-jwt}
            - {name: SHIZUHA_JWT_SIGNING_KEY, value: staging-only-jwt}
            - {name: HIVE_TOKEN_COORDINATOR_KEK, value: staging-only-kek}
            - {name: HIVE_TOKEN_COORDINATOR_KEK_ID, value: staging-v1}
            - {name: DEBUG, value: "True"}
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {cpu: "1", memory: 1Gi}
---
apiVersion: v1
kind: Service
metadata:
  name: hive
  namespace: staging
  labels: {service: hive, release_ring: staging, build_sha: __BUILD_SHA__}
spec:
  selector: {app: hive, release_ring: staging, build_sha: __BUILD_SHA__}
  ports: [{name: http, port: 8030, targetPort: http}]
---
apiVersion: monitoring.coreos.com/v1
kind: Probe
metadata:
  name: probe-shizuha-hive-staging-__BUILD_SHORT__
  namespace: monitoring
  labels:
    release: kube-prometheus
    app.kubernetes.io/component: synthetic-probes
    service: hive
    release_ring: staging
    build_sha: __BUILD_SHA__
    shizuha.io/task: PLAT-4086
spec:
  jobName: shizuha-hive-staging-__BUILD_SHORT__
  prober:
    url: blackbox-exporter.monitoring.svc.cluster.local:9115
    scheme: http
    path: /probe
  module: http_2xx_insecure
  interval: 30s
  scrapeTimeout: 15s
  targets:
    staticConfig:
      static: [http://hive.staging.svc.cluster.local:8030/api/health/]
      labels:
        service: hive
        release_ring: staging
        build_sha: __BUILD_SHA__
        team: engineering
