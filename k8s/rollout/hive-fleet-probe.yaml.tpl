apiVersion: monitoring.coreos.com/v1
kind: Probe
metadata:
  name: probe-shizuha-hive-fleet-__BUILD_SHORT__
  namespace: monitoring
  labels:
    release: kube-prometheus
    app.kubernetes.io/component: synthetic-probes
    service: hive
    release_ring: fleet
    build_sha: __BUILD_SHA__
    shizuha.io/task: PLAT-4086
spec:
  jobName: shizuha-hive-fleet-__BUILD_SHORT__
  prober:
    url: blackbox-exporter.monitoring.svc.cluster.local:9115
    scheme: http
    path: /probe
  module: http_2xx_insecure
  interval: 30s
  scrapeTimeout: 15s
  targets:
    staticConfig:
      static: [http://hive.shizuha-hive.svc.cluster.local:8030/api/health/]
      labels:
        service: hive
        release_ring: fleet
        build_sha: __BUILD_SHA__
        team: engineering
