# PLAT-4086 R0 isolated dependencies. These objects contain no production
# Secret/ConfigMap references and are safe to recreate for every staging build.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hive-staging-postgres
  namespace: staging
  labels: {app: hive-staging-postgres, release_ring: staging, build_sha: __BUILD_SHA__}
spec:
  replicas: 1
  selector: {matchLabels: {app: hive-staging-postgres}}
  template:
    metadata:
      labels: {app: hive-staging-postgres, release_ring: staging}
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          env:
            - {name: POSTGRES_DB, value: shizuha_hive_staging}
            - {name: POSTGRES_USER, value: postgres}
            - {name: POSTGRES_PASSWORD, value: staging-only}
          ports: [{name: postgres, containerPort: 5432}]
          readinessProbe:
            exec: {command: [pg_isready, -U, postgres, -d, shizuha_hive_staging]}
            periodSeconds: 3
            timeoutSeconds: 2
          resources:
            requests: {cpu: 50m, memory: 128Mi}
            limits: {cpu: 500m, memory: 512Mi}
---
apiVersion: v1
kind: Service
metadata:
  name: hive-staging-postgres
  namespace: staging
  labels: {release_ring: staging, build_sha: __BUILD_SHA__}
spec:
  selector: {app: hive-staging-postgres}
  ports: [{name: postgres, port: 5432, targetPort: postgres}]
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hive-staging-redis
  namespace: staging
  labels: {app: hive-staging-redis, release_ring: staging, build_sha: __BUILD_SHA__}
spec:
  replicas: 1
  selector: {matchLabels: {app: hive-staging-redis}}
  template:
    metadata:
      labels: {app: hive-staging-redis, release_ring: staging}
    spec:
      containers:
        - name: redis
          image: redis:7-alpine
          args: [redis-server, --save, "", --appendonly, "no"]
          ports: [{name: redis, containerPort: 6379}]
          readinessProbe:
            exec: {command: [redis-cli, ping]}
            periodSeconds: 3
            timeoutSeconds: 2
          resources:
            requests: {cpu: 25m, memory: 32Mi}
            limits: {cpu: 250m, memory: 128Mi}
---
apiVersion: v1
kind: Service
metadata:
  name: hive-staging-redis
  namespace: staging
  labels: {release_ring: staging, build_sha: __BUILD_SHA__}
spec:
  selector: {app: hive-staging-redis}
  ports: [{name: redis, port: 6379, targetPort: redis}]
