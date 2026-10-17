"""PLAT-7725 regression contract: the on-demand reconciler CronJob must be
pinned by IMMUTABLE DIGEST, never a mutable tag.

The 09-03→09-04 rollback churn re-tagged the fleet image and the
previously-rendered ``hive-f12447b`` tag was overwritten in the registry, so
the CI-rendered CronJob sat dark in ImagePullBackOff — every 10m tick added
another failed pod and no fire could start. The fleet-release renderer now
resolves the live hive image's registry digest and pins the CronJob to
``repo@sha256:...``; this test battery locks that contract in.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLEET_SCRIPT = ROOT / ".forgejo" / "scripts" / "ring-release-fleet.sh"
BOOTSTRAP_MANIFEST = ROOT / "k8s" / "hive-on-demand-reconciler.yaml"


class OnDemandReconcilerDigestPinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = FLEET_SCRIPT.read_text()

    def test_render_resolves_live_image_digest(self):
        self.assertIn("ON_DEMAND_DIGEST", self.script)
        self.assertIn("Docker-Content-Digest", self.script.replace("docker-content-digest", "Docker-Content-Digest"))
        self.assertIn("/v2/shizuha-hive/manifests/", self.script)

    def test_digest_resolution_is_digest_aware(self):
        # The registry must be asked with OCI index / manifest list Accept
        # headers, otherwise multi-arch tags 404 (MANIFEST_UNKNOWN).
        self.assertIn("application/vnd.oci.image.index.v1+json", self.script)
        self.assertIn("application/vnd.docker.distribution.manifest.list.v2+json", self.script)

    def test_unresolvable_digest_fails_loud(self):
        self.assertIn("refusing to render the on-demand reconciler against a mutable tag", self.script)
        self.assertRegex(self.script, r"\[\[ \"\$\{ON_DEMAND_DIGEST\}\" =~ \^sha256:\[0-9a-f\]\{64\}\$ \]\]")

    def test_rendered_cronjob_image_comes_from_digest_pin(self):
        # The python render block must override the copied live container
        # image with the digest-pinned ref — never inherit the mutable tag.
        self.assertIn('container["image"] = os.environ["ON_DEMAND_IMAGE"]', self.script)
        self.assertIn("export ON_DEMAND_IMAGE", self.script)

    def test_digest_pin_survives_already_pinned_live_image(self):
        # If the live Deployment is itself digest-pinned, keep it verbatim.
        self.assertIn("*@sha256:*)", self.script)

    def test_bootstrap_manifest_stays_reference_only(self):
        text = BOOTSTRAP_MANIFEST.read_text()
        self.assertIn("BOOTSTRAP REFERENCE ONLY", text)
        self.assertIn("CI-owned", text)


if __name__ == "__main__":
    unittest.main()
