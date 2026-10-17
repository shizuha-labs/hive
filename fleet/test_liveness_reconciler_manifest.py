from pathlib import Path

import yaml
from django.test import SimpleTestCase


class FleetAgentLivenessReconcilerManifestTests(SimpleTestCase):
    def test_activity_timestamp_uses_fleet_naive_utc_clock(self):
        manifest = list(yaml.safe_load_all(
            Path('k8s/fleet-agent-liveness-reconciler.yaml').read_text()
        ))
        config_map = next(doc for doc in manifest if doc.get('kind') == 'ConfigMap')
        script = config_map['data']['reconcile_liveness.py']

        self.assertIn('from fleet.timeutils import naive_utc', script)
        self.assertIn('measured = naive_utc(datetime.datetime.fromtimestamp(max(activity_times)', script)
        self.assertNotIn('measured = _djtz.now()', script)
        self.assertIn('ACTIVITY_UPDATE_ERROR', script)

    def test_liveness_reads_shizuha_log_not_just_kube_stdout(self):
        manifest = list(yaml.safe_load_all(
            Path('k8s/fleet-agent-liveness-reconciler.yaml').read_text()
        ))
        config_map = next(doc for doc in manifest if doc.get('kind') == 'ConfigMap')
        script = config_map['data']['reconcile_liveness.py']

        self.assertIn('file_log_signal', script)
        self.assertIn('remote_probe_source()', script)
        self.assertIn('FILELOG_ERROR', script)
        self.assertIn('no-activity-signal', script)
        self.assertIn('fail-open', script)
        self.assertIn('kube_log_signal', script)
        # Missing kube logs must NOT be treated as a full-window silent wedge.
        self.assertNotIn('age = LOG_LOOKBACK', script)

    def test_fleet_release_owns_exact_liveness_controller_image_and_script(self):
        release = Path('.forgejo/scripts/ring-release-fleet.sh').read_text()

        self.assertIn('Installing fleet-agent liveness reconciliation CronJob', release)
        self.assertIn('k8s/fleet-agent-liveness-reconciler.yaml', release)
        self.assertIn('LIVENESS_CRON_IMAGE', release)
        self.assertIn('fleet-agent liveness image drift', release)
        self.assertIn('grep -q ACTIVITY_UPDATE_ERROR', release)
