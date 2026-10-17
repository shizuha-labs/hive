"""Exercise the real ENOSPC branch with an inert Docker transport."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class LocalGateCleanupScopeTests(unittest.TestCase):
    def test_enospc_diagnoses_without_deleting_other_invocations_resources(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'docker.log'
            binary = root / 'bin'
            binary.mkdir()
            scripts = {
                'docker': '''#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_TEST_LOG"
case "$1 $2" in
  'exec '*) exit 1 ;;
  'logs '*) echo 'FATAL: no space left on device'; exit 0 ;;
  'info '*) echo /tmp/docker ;;
esac
exit 0
''',
                'git': '#!/bin/sh\nprintf "%s\\n" "$SOURCE_ROOT"\n',
                'flock': '#!/bin/sh\nexit 0\n',
                'seq': '#!/bin/sh\necho 1\n',
                'sleep': '#!/bin/sh\nexit 0\n',
                'df': '#!/bin/sh\necho "diagnostic: full disposable Docker filesystem"\n',
            }
            for name, body in scripts.items():
                file = binary / name
                file.write_text(body)
                file.chmod(0o755)
            env = {**os.environ, 'PATH':str(binary)+os.pathsep+os.environ['PATH'],
                   'SOURCE_ROOT':str(source), 'DOCKER_TEST_LOG':str(log), 'SHIZUHA_CI_IMAGE':'fixture-image'}
            run = subprocess.run(['bash',str(source/'.ci-local-gate.sh')], env=env,
                                 capture_output=True,text=True,timeout=10)
            self.assertNotEqual(run.returncode,0)
            self.assertIn('ENOSPC',run.stderr)
            operations = log.read_text().splitlines()
            self.assertTrue(any(op.startswith('rm -f hive-gate-pg-') for op in operations))
            for op in operations:
                self.assertFalse(op.startswith(('buildx ', 'container prune', 'volume ls', 'ps -aq')),op)
                if op.startswith('volume rm '):
                    self.assertEqual(op,'volume rm hive-gate-pgdata')


if __name__ == '__main__':
    unittest.main()
