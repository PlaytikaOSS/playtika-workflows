"""Exercise the actual release policy and artifact processing from the workflow."""

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

import yaml


WORKFLOW = yaml.safe_load((Path(__file__).parents[1] / 'workflows/maven-central-release.yml').read_text())
SHA = 'a' * 40
TAG = '5.0.0'
ROOT_POM = f'com/playtika/reactivefeign/feign-reactor/{TAG}/feign-reactor-{TAG}.pom'
CORE_JAR = f'com/playtika/reactivefeign/feign-reactor-core/{TAG}/feign-reactor-core-{TAG}.jar'


def step(job, name):
    return next(step for step in WORKFLOW['jobs'][job]['steps'] if step['name'] == name)


def python_script(name, job='publish'):
    command = step(job, name)['run']
    return compile(command.split('\n', 1)[1].rsplit('\nPY', 1)[0], name, 'exec')


class SourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = {
            'context': {'eventName': 'release', 'actor': 'maintainer',
                        'repo': {'owner': 'PlaytikaOSS', 'repo': 'feign-reactive'},
                        'ref': 'refs/tags/' + TAG, 'sha': SHA,
                        'payload': {'action': 'published', 'release': {
                            'id': 1, 'tag_name': TAG, 'draft': False, 'prerelease': False,
                            'author': {'login': 'maintainer'}}}},
            'object': {'type': 'commit', 'sha': SHA},
            'tag_objects': [], 'comparison': 'ahead', 'protected': True,
            'environment': {
                'protection_rules': [{'type': 'required_reviewers', 'prevent_self_review': True,
                                      'reviewers': [{'type': 'Team', 'reviewer': {'id': 1}}]}],
                'deployment_branch_policy': {'custom_branch_policies': True}},
            'policies': {'total_count': 1, 'branch_policies': [
                {'type': 'tag', 'name': '[0-9]*.[0-9]*.[0-9]*'}]},
        }
        self.fixture['release'] = copy.deepcopy(self.fixture['context']['payload']['release'])
        self.env = {'RELEASE_ACTORS': 'maintainer, approved-bot', 'TRIGGERING_ACTOR': 'maintainer',
                    'WORKFLOW_SHA': SHA, 'SHARED_WORKFLOW_SHA': 'b' * 40,
                    'RELEASE_BRANCH': 'develop', 'GROUP_ID': 'com.playtika.reactivefeign'}

    def run_policy(self, approved=True):
        fixture = copy.deepcopy(self.fixture)
        fixture['script'] = step('validate', 'Validate release authorization and source')['with']['script']
        harness = r"""
          const f = JSON.parse(require('fs').readFileSync(0, 'utf8'));
          const outputs = {};
          const core = {setOutput: (key, value) => outputs[key] = value,
            summary: {addHeading() {return this}, addTable() {return this}, async write() {}}};
          const github = {rest: {git: {
            getRef: async args => {
              if (args.ref !== `tags/${f.release.tag_name}`) throw Error('Wrong tag lookup');
              return {data: {object: f.object}};
            },
            getTag: async () => ({data: {object: f.tag_objects.shift()}})
          }, repos: {
            getRelease: async () => ({data: f.release}),
            getEnvironment: async () => {
              if (f.environment_error) throw Error('Environment not accessible');
              return {data: f.environment};
            },
            listDeploymentBranchPolicies: async () => ({data: f.policies}),
            getBranch: async args => {
              if (args.branch !== process.env.RELEASE_BRANCH) throw Error('Wrong protected branch');
              return {data: {protected: f.protected}};
            },
            compareCommits: async args => {
              if (args.base !== f.context.sha || args.head !== process.env.RELEASE_BRANCH) throw Error('Wrong ancestry check');
              return {data: {status: f.comparison}};
            }
          }}};
          (async () => {
            try {
              const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
              await new AsyncFunction('github', 'context', 'core', f.script)(github, f.context, core);
              process.stdout.write(JSON.stringify({approved: true, outputs}));
            } catch (error) {
              process.stdout.write(JSON.stringify({approved: false, error: error.message, outputs}));
            }
          })();
        """
        result = subprocess.run(['node', '-e', harness], input=json.dumps(fixture),
                                text=True, capture_output=True, check=True,
                                env={**os.environ, **self.env})
        result = json.loads(result.stdout)
        self.assertEqual(result['approved'], approved, result)
        if approved:
            self.assertEqual(result['outputs'], {'sha': SHA, 'tag': TAG})
        else:
            self.assertEqual(result['outputs'], {})

    def test_lightweight_and_annotated_tags(self):
        self.run_policy()
        self.fixture['object'] = {'type': 'tag', 'sha': 'b' * 40}
        self.fixture['tag_objects'] = [{'type': 'commit', 'sha': SHA}]
        self.fixture['comparison'] = 'identical'
        self.run_policy()

    def test_other_playtika_repositories_and_protected_branches(self):
        self.fixture['context']['repo']['repo'] = 'testcontainers-spring-boot'
        self.env['RELEASE_BRANCH'] = 'main'
        self.env['GROUP_ID'] = 'com.playtika.testcontainers'
        self.run_policy()

    def test_workflow_pin_and_maven_group_required(self):
        self.env['SHARED_WORKFLOW_SHA'] = 'main'
        self.run_policy(False)
        self.env['SHARED_WORKFLOW_SHA'] = 'b' * 40
        self.env['GROUP_ID'] = 'com.attacker'
        self.run_policy(False)

    def test_dispatch_draft_prerelease_and_fork_rejected(self):
        original = copy.deepcopy(self.fixture)
        for mutation in ['dispatch', 'draft', 'prerelease', 'fork']:
            with self.subTest(mutation=mutation):
                self.fixture = copy.deepcopy(original)
                if mutation == 'dispatch':
                    self.fixture['context']['eventName'] = 'workflow_dispatch'
                elif mutation == 'fork':
                    self.fixture['context']['repo']['owner'] = 'writer'
                else:
                    self.fixture['context']['payload']['release'][mutation] = True
                self.run_policy(False)

    def test_all_actors_must_be_authorized_including_reruns(self):
        self.env['RELEASE_ACTORS'] = ''
        self.run_policy(False)
        self.env['RELEASE_ACTORS'] = 'maintainer'
        self.fixture['context']['actor'] = 'writer'
        self.run_policy(False)
        self.fixture['context']['actor'] = 'maintainer'
        self.env['TRIGGERING_ACTOR'] = 'writer'
        self.run_policy(False)
        self.env['TRIGGERING_ACTOR'] = 'maintainer'
        self.fixture['release']['author']['login'] = 'writer'
        self.run_policy(False)

    def test_tag_sha_and_workflow_must_match(self):
        self.fixture['object']['sha'] = 'b' * 40
        self.run_policy(False)
        self.fixture['object']['sha'] = SHA
        self.env['WORKFLOW_SHA'] = 'b' * 40
        self.run_policy(False)
        self.env['WORKFLOW_SHA'] = SHA
        self.fixture['object']['type'] = 'tree'
        self.run_policy(False)

    def test_mutable_refs_and_invalid_versions_rejected(self):
        original = copy.deepcopy(self.fixture)
        for tag in ['develop', 'v5.0.0', '5.0.0-SNAPSHOT', '05.0.0', '5.0.0\n', '$(id)']:
            with self.subTest(tag=tag):
                self.fixture = copy.deepcopy(original)
                self.fixture['context']['payload']['release']['tag_name'] = tag
                self.fixture['release']['tag_name'] = tag
                self.fixture['context']['ref'] = 'refs/tags/' + tag
                self.run_policy(False)
        self.fixture = original
        self.fixture['context']['ref'] = 'refs/heads/develop'
        self.run_policy(False)

    def test_changed_release_and_unapproved_branch_rejected(self):
        self.fixture['release']['draft'] = True
        self.run_policy(False)
        self.fixture['release']['draft'] = False
        for comparison in ['behind', 'diverged']:
            self.fixture['comparison'] = comparison
            self.run_policy(False)
        self.fixture['comparison'] = 'ahead'
        self.fixture['protected'] = False
        self.run_policy(False)

    def test_missing_environment_and_approvals_fail_closed(self):
        self.fixture['environment_error'] = True
        self.run_policy(False)
        del self.fixture['environment_error']
        self.fixture['environment']['protection_rules'][0]['prevent_self_review'] = False
        self.run_policy(False)
        self.fixture['environment']['protection_rules'][0]['prevent_self_review'] = True
        self.fixture['environment']['protection_rules'][0]['reviewers'] = []
        self.run_policy(False)

    def test_environment_must_only_allow_version_tags(self):
        for policies in [[], [{'type': 'branch', 'name': 'develop'}], [{'type': 'tag', 'name': '*'}]]:
            with self.subTest(policies=policies):
                self.fixture['policies'] = {'total_count': len(policies), 'branch_policies': policies}
                self.run_policy(False)

    def test_excessive_annotated_tag_nesting_rejected(self):
        self.fixture['object'] = {'type': 'tag', 'sha': 'b' * 40}
        self.fixture['tag_objects'] = [self.fixture['object']] * 9
        self.run_policy(False)


class WorkflowBoundaryTests(unittest.TestCase):
    def test_reusable_workflow_scopes_credentials(self):
        trigger = WORKFLOW.get('on', WORKFLOW.get(True))
        self.assertEqual(set(trigger), {'workflow_call'})
        self.assertEqual(WORKFLOW['permissions'], {})
        jobs = WORKFLOW['jobs']
        self.assertEqual(jobs['publish']['environment']['name'], 'maven-central')
        self.assertEqual(set(jobs['publish']['needs']), {'validate', 'build', 'attest'})
        self.assertEqual(step('publish', 'Recheck authorization and source after environment approval')['with']['script'],
                         step('validate', 'Validate release authorization and source')['with']['script'])
        for job_name, job in jobs.items():
            self.assertTrue(all(level == 'read' or job_name == 'attest' for level in job['permissions'].values()))
            for release_step in job['steps']:
                if 'uses' in release_step:
                    self.assertRegex(release_step['uses'], r'@[a-f0-9]{40}$')
                if job_name != 'publish':
                    self.assertNotIn('secrets.', json.dumps(release_step))
                if job_name in {'publish', 'attest'}:
                    self.assertNotIn('actions/checkout', release_step.get('uses', ''))
                    self.assertNotIn('./mvnw', release_step.get('run', ''))
        checkout = step('build', 'Checkout validated commit')['with']
        self.assertEqual(checkout['ref'], '${{ needs.validate.outputs.sha }}')
        self.assertIs(checkout['persist-credentials'], False)

    def test_official_jreleaser_action_runs_after_verification(self):
        steps = WORKFLOW['jobs']['publish']['steps']
        names = [entry['name'] for entry in steps]
        verify = 'Verify attested digest and source before accessing secrets'
        publish = 'Sign and publish verified artifacts with JReleaser'
        self.assertLess(names.index(verify), names.index(publish))
        command = step('publish', verify)['run']
        for option in ['sha256sum --check --strict', '--signer-workflow', '--signer-digest',
                       '--source-digest', '--source-ref', '--deny-self-hosted-runners']:
            self.assertIn(option, command)
        action = step('publish', publish)
        self.assertTrue(action['uses'].startswith('jreleaser/release-action@'))
        self.assertEqual(action['with']['version'], '1.26.0')
        self.assertEqual(action['with']['arguments'], 'deploy --config-file=jreleaser.json --strict')


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'unsigned').mkdir()
        self.env = {'RUNNER_TEMP': str(self.root), 'RELEASE_TAG': TAG,
                    'GROUP_ID': 'com.playtika.reactivefeign', 'GITHUB_REPOSITORY': 'PlaytikaOSS/feign-reactive'}
        self.script = python_script('Prepare verified artifacts and fixed JReleaser configuration')

    def archive(self, entries):
        with warnings.catch_warnings(), zipfile.ZipFile(self.root / 'unsigned/central-bundle.zip', 'w') as archive:
            warnings.simplefilter('ignore', UserWarning)
            for path, data in entries:
                archive.writestr(path, data)

    def test_unsafe_bundles_rejected_before_extraction(self):
        link = zipfile.ZipInfo(CORE_JAR)
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entries in [[], [(ROOT_POM, b'pom'), (ROOT_POM, b'duplicate')],
                        [('../../escape.pom', b'attack')], [(CORE_JAR, b'jar')],
                        [(ROOT_POM, b'pom'), (link, b'/tmp/escape')],
                        [(ROOT_POM.replace(TAG, '9.0.0'), b'pom')],
                        [(ROOT_POM, b'pom'), ('repository/script.py', b'attack')],
                        [(ROOT_POM, b'pom'), ('jreleaser.yml', b'hooks: malicious')]]:
            with self.subTest(entries=str(entries)), patch.dict(os.environ, self.env):
                self.archive(entries)
                with self.assertRaises(SystemExit):
                    exec(self.script, {})
                self.assertFalse((self.root / 'publisher').exists())

    def test_artifacts_are_data_and_publisher_configuration_is_fixed(self):
        artifacts = {ROOT_POM: b'<project>untrusted POM data</project>', CORE_JAR: b'jar data'}
        self.archive(artifacts.items())
        with patch.dict(os.environ, self.env):
            exec(self.script, {})
        publisher = self.root / 'publisher'
        for name, data in artifacts.items():
            self.assertEqual((publisher / 'staging' / name).read_bytes(), data)
        config = json.loads((publisher / 'jreleaser.json').read_text())
        self.assertNotIn('hooks', config)
        self.assertEqual(config['project']['languages']['java']['groupId'], 'com.playtika.reactivefeign')
        self.assertTrue(config['signing']['pgp']['verify'])
        self.assertTrue(config['deploy']['maven']['mavenCentral']['sonatype']['applyMavenCentralRules'])
        self.assertIs(config['deploy']['maven']['mavenCentral']['sonatype']['verifyPom'], False)
        self.assertEqual(config['deploy']['maven']['mavenCentral']['sonatype']['url'],
                         'https://central.sonatype.com/api/v1/publisher')


class UnsignedBundleTests(unittest.TestCase):
    def test_maven_layout_and_digest(self):
        script = python_script('Bundle unsigned artifacts', job='build')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / 'central-staging'
            for name, data in [(ROOT_POM, b'pom'), (CORE_JAR, b'jar'),
                               (ROOT_POM + '.sha1', b'old checksum'),
                               ('com/playtika/reactivefeign/feign-reactor/maven-metadata.xml', b'metadata')]:
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            output = root / 'output'
            with patch.dict(os.environ, {'RUNNER_TEMP': str(root), 'GITHUB_OUTPUT': str(output)}):
                exec(script, {})
            bundle = root / 'central-bundle.zip'
            with zipfile.ZipFile(bundle) as archive:
                self.assertEqual(set(archive.namelist()), {ROOT_POM, CORE_JAR})
            self.assertEqual(output.read_text(), 'sha256=' + hashlib.sha256(bundle.read_bytes()).hexdigest() + '\n')


class ArtifactVerificationTests(unittest.TestCase):
    def test_altered_bundle_fails_before_attestation_verification(self):
        command = step('publish', 'Verify attested digest and source before accessing secrets')['run']
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'unsigned').mkdir()
            (root / 'unsigned/central-bundle.zip').write_bytes(b'altered bundle')
            environment = {**os.environ, 'RUNNER_TEMP': temporary,
                           'BUNDLE_SHA256': hashlib.sha256(b'approved bundle').hexdigest(),
                           'GITHUB_REPOSITORY': 'PlaytikaOSS/feign-reactive', 'RELEASE_SHA': SHA,
                           'RELEASE_TAG': TAG, 'SHARED_WORKFLOW_SHA': 'b' * 40}
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', command],
                                    env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('FAILED', result.stdout)


if __name__ == '__main__':
    unittest.main()
