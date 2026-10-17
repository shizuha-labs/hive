"""Run Hive's actual pod launcher against a receipt-only process boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase

from fleet import provisioner


class FleetContextFileLaunchTests(SimpleTestCase):
    def run_launcher(self, prompt, *, broker=False, writable=True):
        with tempfile.TemporaryDirectory() as root:
            scratch = Path(root)
            capture = scratch / 'capture.py'
            capture.write_text(
                'import json,os,pathlib,stat,sys\n'
                'args=sys.argv[1:]\n'
                'file=pathlib.Path(args[args.index("--context-prompt-file")+1]) '
                'if "--context-prompt-file" in args else None\n'
                'print(json.dumps({"argv":args,"text":file.read_text() if file else None,'
                '"mode":stat.S_IMODE(file.stat().st_mode) if file else None,'
                '"file":str(file) if file else None,'
                '"broker_ready":pathlib.Path(os.environ["RECEIPT"]).exists()}))\n'
            )
            curl = scratch / 'curl'
            curl.write_text('#!/bin/sh\nprintf ready > "$RECEIPT"\n')
            curl.chmod(0o700)
            with (
                patch.object(provisioner, '_uses_native_runtime_image', return_value=True),
                patch.object(provisioner, '_resolve_bridge', return_value='gateway'),
                patch.object(provisioner, 'DEFAULT_AGENT_ENTRYPOINT', [sys.executable, str(capture)]),
            ):
                command = provisioner._agent_launch_command(object(), wait_for_broker=broker)
            env = {
                'PATH': str(scratch) + os.pathsep + os.defpath,
                'TMPDIR': root if writable else str(scratch / 'absent'),
                'RECEIPT': str(scratch / 'broker-ready'),
                'CONTEXT_PROMPT': prompt,
                'AGENT_ID': 'test-id', 'AGENT_USERNAME': 'test-agent',
            }
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
            if not writable:
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')  # runtime was never started
                return
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertNotIn('--context-prompt', receipt['argv'])
            self.assertEqual(receipt['broker_ready'], broker)
            if prompt:
                self.assertEqual(receipt['text'], prompt)
                self.assertEqual(receipt['mode'], 0o600)
                self.assertEqual(Path(receipt['file']).parent, scratch)
                self.assertNotIn(prompt, receipt['argv'])
            else:
                self.assertIsNone(receipt['file'])
                self.assertNotIn('--context-prompt-file', receipt['argv'])

    def test_multiline_and_large_documents_reach_runtime_unchanged(self):
        for broker in (False, True):
            for prompt in ('Role\nTools\tand Unicode: 雪\n', 'skill body\n' * 1000):
                with self.subTest(broker=broker, size=len(prompt)):
                    self.run_launcher(prompt, broker=broker)

    def test_shell_metacharacters_are_literal_document_data(self):
        self.run_launcher('$(exit 9) `exit 8` "quoted" \\ backslash\nlast line\n')

    def test_omitted_context_creates_no_file_argument(self):
        self.run_launcher('')

    def test_failed_file_creation_never_starts_runtime(self):
        self.run_launcher('Role\nTools', writable=False)
