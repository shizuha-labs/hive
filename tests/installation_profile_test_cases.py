"""Real shell caller regressions for the optional installation profile."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml


ENV_NAMES = (
    'HIVE_RUNTIME_HOST_CLI', 'HIVE_RUNTIME_BASE_IMAGE', 'HIVE_RUNTIME_BROKER_IMAGE',
    'HIVE_RUNTIME_AGENT_NODE_SELECTOR', 'HIVE_RUNTIME_DAEMON_NODE_SELECTOR',
    'HIVE_HOSTPLANE_SSH_TAILNET_CIDRS', 'HIVE_HOSTPLANE_SSH_BOOTSTRAP_CIDRS',
    'SHIZUHA_PULSE_HUMAN_EMAILS',
)


class InstallationProfileCases:
    def installation_profile_role(self):
        root = Path(__file__).resolve().parents[1]
        documents = list(yaml.safe_load_all((root/'k8s/hive-ring-executor-rbac.yaml').read_text()))
        roles = [item for item in documents if item and item.get('kind') == 'Role'
                 and item.get('metadata') == {'name': 'hive-ring-execute', 'namespace': 'shizuha-hive'}]
        self.assertEqual(len(roles), 1)
        bindings = [item for item in documents if item and item.get('kind') == 'RoleBinding'
                    and item.get('metadata') == {'name': 'hive-ring-execute', 'namespace': 'shizuha-hive'}]
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]['roleRef'], {'apiGroup': 'rbac.authorization.k8s.io',
                                                'kind': 'Role', 'name': 'hive-ring-execute'})
        self.assertEqual(bindings[0]['subjects'], [{'kind': 'ServiceAccount',
                                                  'name': 'hive-ring-execute', 'namespace': 'origin'}])
        return roles[0]

    def installation_profile_run(self, mode='present', *, native_ring=False, role_rules=None):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            log = temp/'calls.jsonl'
            patches = temp/'patches.jsonl'
            fake = temp/'kubectl'
            fake.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['PROFILE_CALLS'],'a') as handle:handle.write(json.dumps(args)+'\\n')
mode=os.environ['PROFILE_MODE']
if args[:4]==['-n','shizuha-hive','get','secret']:
 assert args[4]=='hive-installation-config'
 if os.environ.get('PROFILE_SOURCE_RBAC')=='1':
  rules=json.loads(os.environ['PROFILE_ROLE_RULES'])
  permitted=any('' in rule.get('apiGroups',[]) and 'secrets' in rule.get('resources',[])
                and 'get' in rule.get('verbs',[]) and
                (not rule.get('resourceNames') or args[4] in rule['resourceNames']) for rule in rules)
  if not permitted:
   print('Forbidden: executor cannot get named installation Secret',file=sys.stderr);sys.exit(1)
 if any('go-template=' in arg for arg in args):
  names=['HIVE_RUNTIME_HOST_CLI', 'HIVE_RUNTIME_BASE_IMAGE', 'HIVE_RUNTIME_BROKER_IMAGE', 'HIVE_RUNTIME_AGENT_NODE_SELECTOR', 'HIVE_RUNTIME_DAEMON_NODE_SELECTOR', 'HIVE_HOSTPLANE_SSH_TAILNET_CIDRS', 'HIVE_HOSTPLANE_SSH_BOOTSTRAP_CIDRS', 'SHIZUHA_PULSE_HUMAN_EMAILS']
  if mode=='partial-profile':names.pop()
  print('\\n'.join(names));sys.exit(0)
 assert '--ignore-not-found' in args
 assert any('metadata.uid' in arg for arg in args)
 assert not any('.data' in arg for arg in args)
 if mode=='read-error':sys.exit(1)
 if mode!='absent':print('fixture-secret-uid')
 sys.exit(0)
if args[:4]==['-n','shizuha-hive','get','deploy']:
 name=args[4]
 if mode=='missing-container' and name=='hive-api':print('other-sidecar');sys.exit(0)
 print(name+' unrelated-sidecar');sys.exit(0)
if args[:3]==['-n','shizuha-hive','patch']:
 assert args[3] in ('deploy/hive','deploy/hive-api')
 assert '--type=strategic' in args
 filename=next(arg.split('=',1)[1] for arg in args if arg.startswith('--patch-file='))
 payload=json.loads(Path(filename).read_text())
 with open(os.environ['PROFILE_PATCHES'],'a') as handle:handle.write(json.dumps({'target':args[3],'patch':payload})+'\\n')
 if mode=='patch-error':sys.exit(1)
 sys.exit(0)
raise SystemExit('unexpected kubectl invocation')
''')
            fake.chmod(0o755)
            environment={**os.environ,'KUBECTL':str(fake),'PROFILE_MODE':mode,
                         'PROFILE_CALLS':str(log),'PROFILE_PATCHES':str(patches),
                         'PROFILE_SOURCE_RBAC':'1' if native_ring else '0',
                         'PROFILE_ROLE_RULES':json.dumps(role_rules if role_rules is not None
                                                        else self.installation_profile_role()['rules'])}
            if native_ring:
                source=(root/'.forgejo/scripts/ring-release-fleet.sh').read_text()
                marker="trap 'r2_failure 130 \"signal=INT\"' INT\n"
                # Execute the native pre-rollout boundary, not a copied helper call.
                boundary=source.split(marker,1)[1].split('.forgejo/scripts/kubectl -n shizuha-hive patch deploy/hive --type=merge',1)[0]
                result=subprocess.run(['bash','-euo','pipefail','-c',boundary],cwd=root,env=environment,text=True,capture_output=True)
            else:
                result=subprocess.run(['bash',str(root/'.forgejo/scripts/preserve-installation-profile.sh')],cwd=root,env=environment,text=True,capture_output=True)
            calls=[json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            captured=[json.loads(line) for line in patches.read_text().splitlines()] if patches.exists() else []
            return result,calls,captured

    def test_native_ring_preserves_installation_profile_before_other_rollout(self):
        result,calls,patches=self.installation_profile_run(native_ring=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual([item['target'] for item in patches],['deploy/hive','deploy/hive-api'])

    def test_native_ring_missing_named_read_fails_before_deployment_access(self):
        rules = self.installation_profile_role()['rules']
        for rule in rules:
            if 'secrets' in rule.get('resources', []):
                rule['resourceNames'] = [name for name in rule.get('resourceNames', [])
                                         if name != 'hive-installation-config']
        result, calls, patches = self.installation_profile_run(native_ring=True, role_rules=rules)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Forbidden', result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(patches, [])

    def test_executor_installation_read_is_exact_named_get(self):
        role = self.installation_profile_role()
        secret_rules = [rule for rule in role['rules'] if 'secrets' in rule.get('resources', [])]
        self.assertEqual(secret_rules, [{'apiGroups': [''], 'resources': ['secrets'],
                                        'resourceNames': ['hive-connect-hmac', 'hive-installation-config'],
                                        'verbs': ['get']}])

    def test_installation_profile_patch_is_strategic_exact_and_clears_old_sources(self):
        result,calls,patches=self.installation_profile_run()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(patches),2)
        for item in patches:
            self.assertEqual(set(item['patch']),{'spec'})
            pod=item['patch']['spec']['template']['spec']
            self.assertEqual(set(pod),{'containers'})
            self.assertEqual(len(pod['containers']),1)
            container=pod['containers'][0]
            self.assertEqual(container['name'],item['target'].split('/')[1])
            self.assertEqual(set(container),{'name','env'})
            self.assertEqual([entry['name'] for entry in container['env']],list(ENV_NAMES))
            for entry in container['env']:
                self.assertIsNone(entry['value'])
                self.assertEqual(entry['valueFrom'],{'configMapKeyRef':None,'fieldRef':None,'resourceFieldRef':None,'secretKeyRef':{
                    'name':'hive-installation-config','key':entry['name'],'optional':False}})

    def test_absent_installation_secret_does_not_read_or_patch_deployments(self):
        result,calls,patches=self.installation_profile_run('absent')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(calls),1)
        self.assertEqual(patches,[])

    def test_installation_secret_metadata_failure_is_not_treated_as_absence(self):
        result,calls,patches=self.installation_profile_run('read-error')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(patches,[])

    def test_both_real_target_containers_are_checked_before_first_patch(self):
        result,calls,patches=self.installation_profile_run('missing-container')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(patches,[])

    def test_installation_profile_patch_failure_is_propagated(self):
        result,calls,patches=self.installation_profile_run('patch-error')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(len(patches),1)

    def test_partial_installation_profile_fails_before_any_deployment_read_or_patch(self):
        result,calls,patches=self.installation_profile_run('partial-profile')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('missing required key SHIZUHA_PULSE_HUMAN_EMAILS',result.stderr)
        self.assertEqual(len(calls),2)
        self.assertEqual(patches,[])
