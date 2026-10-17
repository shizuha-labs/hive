"""Local gate caller: interrupted Django registration -> dirty keepdb -> rearm.

Fast subprocess controls run without Docker. The real PostgreSQL/Django control
runs with SHIZUHA_RUN_LOCAL_GATE_CACHE_INTEGRATION=1 and SHIZUHA_CI_IMAGE set to
an existing Hive CI image. All volumes/containers are private and disposable.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / '.forgejo/scripts/local-gate-test-cache.sh'
CALL = 'source "$1"; shift; run_local_gate_backend "$@"'


class LocalGateCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.env = {**os.environ, 'PATH': str(self.path) + ':' + os.environ['PATH'],
                    'CASE_DIR': str(self.path)}
        docker = self.path / 'docker'
        docker.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
p=Path(os.environ['CASE_DIR']);args=sys.argv[1:]
with (p/'calls').open('a') as f:f.write(json.dumps(args)+'\\n')
if os.environ.get('FAIL_READ') and args[2:4]==['sh','-c'] and 'if [' in args[4]:sys.exit(47)
if 'dropdb' in args:
 if os.environ.get('FAIL_DROP'):sys.exit(48)
 (p/'database').unlink(missing_ok=True)
elif args[2:4]==['rm','-f']:(p/'marker').unlink(missing_ok=True)
elif 'if [' in args[4]:print((p/'marker').read_text() if (p/'marker').exists() else 'absent',end='')
else:
 if os.environ.get('FAIL_MARK'):sys.exit(49)
 (p/'marker').write_text('clean-v1')
''')
        docker.chmod(0o700)

    def run_backend(self, body='echo ran > "$CASE_DIR/ran"; touch "$CASE_DIR/database"', **env):
        return subprocess.run(['bash', '-c', CALL, 'gate-test', str(HELPER),
                               'hive-gate-pg-123', 'bash', '-c', body],
                              env={**self.env, **env}, capture_output=True, text=True)

    def calls(self):
        p = self.path / 'calls'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_unclean_run_resets_then_clean_successor_reuses(self):
        (self.path/'database').touch()
        self.assertEqual(self.run_backend().returncode, 0)
        self.assertEqual(self.run_backend().returncode, 0)
        self.assertEqual(sum('dropdb' in x for x in self.calls()), 1)
        self.assertEqual((self.path/'marker').read_text(), 'clean-v1')

    def test_failed_backend_stays_dirty_then_next_caller_resets(self):
        (self.path/'marker').write_text('clean-v1')
        self.assertEqual(self.run_backend('touch "$CASE_DIR/database"; exit 23').returncode, 23)
        self.assertFalse((self.path/'marker').exists())
        self.assertEqual(self.run_backend().returncode, 0)
        self.assertEqual(sum('dropdb' in x for x in self.calls()), 1)

    def test_actual_signal_does_not_rearm_clean_marker(self):
        (self.path/'marker').write_text('clean-v1')
        r = self.run_backend('touch "$CASE_DIR/database"; kill -TERM $$')
        self.assertEqual(r.returncode, 143)
        self.assertFalse((self.path/'marker').exists())
        self.assertEqual(self.run_backend().returncode, 0)
        self.assertEqual(sum('dropdb' in x for x in self.calls()), 1)

    def test_cache_read_error_refuses_without_reset_or_test(self):
        self.assertEqual(self.run_backend(FAIL_READ='1').returncode, 47)
        self.assertFalse((self.path/'ran').exists())
        self.assertEqual(len(self.calls()), 1)

    def test_failed_reset_never_runs_backend(self):
        self.assertEqual(self.run_backend(FAIL_DROP='1').returncode, 48)
        self.assertFalse((self.path/'ran').exists())
        self.assertFalse((self.path/'marker').exists())

    def test_failed_clean_mark_cannot_be_reported_success(self):
        self.assertEqual(self.run_backend(FAIL_MARK='1').returncode, 49)
        self.assertFalse((self.path/'marker').exists())

    def test_foreign_postgres_name_is_refused_without_docker(self):
        r = subprocess.run(['bash','-c',CALL,'test',str(HELPER),'production-postgres','true'], env=self.env, capture_output=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_gate_routes_existing_full_backend_command_through_cache_caller(self):
        s=(ROOT/'.ci-local-gate.sh').read_text()
        self.assertIn('source .forgejo/scripts/local-gate-test-cache.sh', s)
        self.assertIn('run_local_gate_backend "$PG" docker run ',s)
        self.assertIn('manage.py test --noinput --keepdb',s)
        self.assertIn('trap cleanup EXIT',s)


@unittest.skipUnless(os.environ.get('SHIZUHA_RUN_LOCAL_GATE_CACHE_INTEGRATION') == '1',
                     'opt in to disposable Docker/PostgreSQL/Django caller control')
class ActualDjangoCacheLifecycleTests(unittest.TestCase):
    def test_interrupted_registration_baseline_fails_then_reset_and_warm_reuse_pass(self):
        image=os.environ['SHIZUHA_CI_IMAGE']
        scratch=Path(os.environ.get('SHIZUHA_AGENT_SCRATCH','/mnt/ramdisk/agent-scratch'))
        case=Path(tempfile.mkdtemp(prefix='hive-cache-caller-',dir=scratch))
        suffix=str(os.getpid())+str(int(uuid.uuid4().hex[:6],16))
        pg='hive-gate-pg-'+suffix; rd='hive-cache-rd-'+suffix; net='hive-cache-net-'+suffix
        def run(args,check=True,timeout=300):
            r=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
            with (case/'commands.log').open('a') as f:
                f.write(json.dumps(args)+'\n'+r.stdout+r.stderr+'\nexit='+str(r.returncode)+'\n')
            if check:self.assertEqual(r.returncode,0,r.stdout[-1000:]+r.stderr[-3000:])
            return r
        def sql(query):
            return run(['docker','exec',pg,'psql','-U','postgres','-d','test_shizuha_hive_ci','-Atc',query]).stdout.strip()
        run(['docker','network','create',net])
        try:
            run(['docker','run','-d','--name',pg,'--network',net,'-v',str(case/'pgdata')+':/var/lib/postgresql/data','-e','POSTGRES_PASSWORD=postgres','-e','POSTGRES_DB=shizuha_hive_ci','postgres:16-alpine'])
            run(['docker','run','-d','--name',rd,'--network',net,'redis:7-alpine'])
            run(['docker','exec',pg,'sh','-c','for i in $(seq 1 30); do pg_isready -U postgres && exit 0; sleep 1; done; exit 1'])
            # Real production test helpers establish the committed registration;
            # SIGTERM prevents TransactionTestCase._post_teardown/flush.
            (case/'cache_interrupt_fixture.py').write_text('''import os,signal
from django.test import TransactionTestCase,override_settings
from fleet.test_hive235_daemon_ws import _ensure_test_credential,_create_nagi_desired,_make_app,_connect_and_register,TEST_CHANNEL_LAYER
@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYER)
class InterruptedRegistration(TransactionTestCase):
 def setUp(self):
  _ensure_test_credential();_create_nagi_desired()
 async def test_register_then_interrupt(self):
  comm,ack=await _connect_and_register(_make_app())
  self.assertEqual(ack['type'],'register_ack')
  os.kill(os.getpid(),signal.SIGTERM)
''')
            prefix=['docker','run','--rm','--init','--network',net,'-v',str(ROOT)+':/source:ro','-v',str(case)+':/case:ro',
                    '-e','DB_NAME=shizuha_hive_ci','-e','DB_USER=postgres','-e','DB_PASSWORD=postgres','-e','DB_HOST='+pg,'-e','DB_PORT=5432',
                    '-e','SECRET_KEY=ci-secret','-e','JWT_SECRET_KEY=ci-secret','-e','DEBUG=True',
                    '-e','REDIS_URL=redis://'+rd+':6379/5','-e','CACHE_REDIS_URL=redis://'+rd+':6379/6','-e','PYTHONPATH=/case',image,'bash','-c']
            def django(label,wrapped):
                body='cp -a /source /work; cd /work; /app/.venv/bin/python manage.py test '+label+' --noinput --keepdb'
                cmd=prefix+[body]
                if wrapped:cmd=['bash','-c',CALL,'integration',str(HELPER),pg]+cmd
                return run(cmd,check=False,timeout=600)
            interrupted=django('cache_interrupt_fixture.InterruptedRegistration.test_register_then_interrupt',True)
            self.assertEqual(interrupted.returncode,143,interrupted.stderr[-2500:])
            self.assertEqual(sql("SELECT daemon_id FROM fleet_daemonregistry"),'daemon-test-001')
            # Original direct --keepdb caller reproduces all three real failures.
            label='fleet.tests.test_hive254_identity_health.DaemonIdentityHealthViewTest'
            baseline=django(label,False)
            self.assertEqual(baseline.returncode,1,baseline.stderr[-2500:])
            self.assertIn('FAILED (failures=3)',baseline.stderr)
            self.assertIn('daemon-test-001',baseline.stderr)
            before=sql("SELECT oid FROM pg_database WHERE datname=current_database()")
            repaired=django(label,True)
            self.assertEqual(repaired.returncode,0,repaired.stderr[-3000:])
            reset=sql("SELECT oid FROM pg_database WHERE datname=current_database()")
            self.assertNotEqual(before,reset)
            warm=django(label,True)
            self.assertEqual(warm.returncode,0,warm.stderr[-3000:])
            self.assertEqual(reset,sql("SELECT oid FROM pg_database WHERE datname=current_database()"))
            self.assertIn('retaining warm test schema',warm.stdout)
            (case/'receipt.json').write_text(json.dumps({'baseline_failures':3,'interrupted_exit':143,'before_db_oid':before,'reset_db_oid':reset,'warm_db_oid':reset,'source':str(ROOT),'image':image,'result':'PASS'},indent=2))
            print('actual Django cache lifecycle receipt:',case,flush=True)
        finally:
            run(['docker','stop','-t','30',pg,rd],check=False)
            run(['docker','rm','-f',pg,rd],check=False)
            run(['docker','network','rm',net],check=False)


if __name__ == '__main__':
    unittest.main()
