"""Isolation/credential tests for the proposed per-repository monitor."""
import base64
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import monitor as m
import delivery as d
import evidence as e
import runtime
from state_store import GitStore
from test_monitor import evidence, run, summary, NOW


def args(target, **changes):
    values = dict(target=target, replay=None, now=None, persist=False, send=False,
                  test_delivery=False, ack_key=None, ack_signature=None,
                  authorization=None, output="unused-test-output")
    return SimpleNamespace(**(values | changes))


class RepositoryIsolation(unittest.TestCase):
    def test_each_scope_rejects_reads_of_other_repositories(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            for key, cfg in m.CONFIG.items():
                with self.subTest(key=key), m.repository_scope(key):
                    self.assertEqual(m.GitHubReader(cfg['repo']).repo, cfg['repo'])
                    for other in set(m.CONFIG) - {key}:
                        with self.assertRaisesRegex(ValueError, "cross_repository_read"):
                            m.GitHubReader(m.CONFIG[other]['repo'])
        self.assertIsNone(m.ACTIVE_REPOSITORY)

    def test_cloud_wrong_target_fails_before_reading(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": m.CONFIG['bizibl']['repo']}), \
             patch.object(m, 'collect') as read:
            with self.assertRaisesRegex(ValueError, 'repository_mismatch'):
                runtime.run(args('takken'))
            read.assert_not_called()

    def test_cloud_uses_only_builtin_token(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_TOKEN': 'builtin-fixture',
               'GH_TOKEN': 'unwanted-personal-fixture', 'MONITOR_GITHUB_TOKEN': 'retired-fixture'}
        with patch.dict(os.environ, env), patch.object(m.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{}')) as call:
            m.GitHubReader(m.CONFIG['bizibl']['repo']).get('actions/workflows/1')
            self.assertEqual(call.call_args.kwargs['env']['GH_TOKEN'], 'builtin-fixture')

    def test_missing_builtin_token_does_not_fall_back_to_personal_token(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_TOKEN': '', 'GH_TOKEN': 'personal-fixture'}), \
             patch.object(m.subprocess, 'run') as call:
            with self.assertRaisesRegex(m.ReadError, 'repository_token_missing'):
                m.GitHubReader(m.CONFIG['bizibl']['repo']).get('actions/workflows/1')
            call.assert_not_called()

    def test_cloud_flags_use_local_context_without_variables_api(self):
        for key in ('takken', 'branded'):
            cfg = m.CONFIG[key]
            paths = []
            def get(reader, path, raw=False):
                paths.append(path)
                self.assertNotIn('variables/', path)
                if path.startswith('contents/'):
                    return '\n'.join('cron: "' + c + '"' for c in cfg['cron'])
                if path.endswith('/runs?branch=main&per_page=100') or 'event=schedule' in path:
                    return {'workflow_runs': []}
                return {'id': cfg['workflow'], 'path': cfg['path'], 'state': 'active', 'created_at': '2026-01-01T00:00:00Z'}
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'MONITOR_AUTOMATION_ENABLED': 'true', 'MONITOR_SCHEDULE_ENABLED': 'false'}), \
                 patch.object(m.GitHubReader, 'get', get):
                observed = m.collect_one(key)
            self.assertEqual(observed['errors'], [])
            self.assertEqual(observed['flags']['AUTOMATION_ENABLED'], 'true')
            if key == 'branded':
                self.assertEqual(observed['flags']['SCHEDULE_ENABLED'], 'false')

    def test_targeted_collection_never_collects_other_workflows(self):
        with patch.object(m, 'collect_one', side_effect=lambda k: {'key': k}) as call:
            snapshot = m.collect('takken')
        call.assert_called_once_with('takken')
        self.assertEqual([x['key'] for x in snapshot['workflows']], ['takken'])

    def test_report_contains_only_selected_target(self):
        snap = {'fetched_at': m.iso(NOW), 'workflows': []}
        for key in m.CONFIG:
            report, _ = m.build_report(snap, target=key)
            self.assertEqual([r['key'] for r in report['results']], [key])
            self.assertEqual(report['results'][0]['state'], 'unknown')

    def test_takken_and_branded_never_need_google_credentials(self):
        for key in ('takken', 'branded'):
            report = {'results': [m.base_result(key, NOW)]}
            check = Mock(side_effect=AssertionError('Unexpected Google access'))
            self.assertIs(e.enrich(report, {'workflows': []}, check_sheets=check, check_downstream=check), report)
            check.assert_not_called()

    def test_journal_destination_is_the_target_repository(self):
        response = Mock(status_code=200)
        response.json.return_value = {'ok': True}
        for key, cfg in m.CONFIG.items():
            with patch('requests.request', return_value=response) as req:
                GitStore('fixture', repository=cfg['repo'], target=key).request('GET', 'contents/alerts.json')
                self.assertEqual(req.call_args.args[1], 'https://api.github.com/repos/'+cfg['repo']+'/contents/alerts.json')

    def test_mixed_ledger_stops_before_any_network_write(self):
        state = d.initial_state()
        r = m.base_result('takken', NOW)
        r.update(state='unknown', condition='read_unavailable')
        state = d.plan(state, {'results': [r]}, NOW)
        store = GitStore('fixture', repository=m.CONFIG['branded']['repo'], target='branded')
        with patch.object(store, 'request') as request:
            with self.assertRaisesRegex(ValueError, 'other_target'):
                store.write('old', state)
            request.assert_not_called()

    def test_mixed_ledger_is_rejected_on_read(self):
        state = d.initial_state()
        state['workflows']['takken'] = {}
        payload = {'sha': 'fixture', 'encoding': 'base64', 'size': 200,
                   'content': base64.b64encode(json.dumps(state).encode()).decode()}
        store = GitStore('fixture', repository=m.CONFIG['bizibl']['repo'], target='bizibl')
        with patch.object(store, 'request', return_value=payload):
            with self.assertRaisesRegex(ValueError, 'other_target'):
                store.read()

    def test_state_scope_mismatch_fails_before_any_request(self):
        with self.assertRaisesRegex(ValueError, 'state_repository_mismatch'):
            GitStore('fixture', repository=m.CONFIG['bizibl']['repo'], target='takken')
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), m.repository_scope('takken'):
            with self.assertRaisesRegex(ValueError, 'cross_repository_write'):
                GitStore('fixture')

    def test_exact_test_cannot_send_from_three_repositories(self):
        for key in ('takken', 'branded'):
            with patch.object(m, 'collect') as read:
                with self.assertRaisesRegex(ValueError, 'one_test_only'):
                    runtime.run(args(key, test_delivery=True))
                read.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'outbox_contains_other_target'):
                d.validate_target(d.enqueue_test(d.initial_state(), NOW), key)

    def test_other_target_policy_is_rejected_before_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)/'policy.json'
            p.write_text(json.dumps({'target': 'bizibl', 'repository': m.CONFIG['bizibl']['repo']}))
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(m, 'collect') as read:
                with self.assertRaisesRegex(ValueError, 'authorization_target_mismatch'):
                    runtime.run(args('takken', send=True, authorization=str(p)))
                read.assert_not_called()

    def test_replay_has_no_other_target_or_external_io(self):
        snap = {'fetched_at': m.iso(NOW), 'workflows': [evidence('takken', [run('takken', summary=summary())])]}
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)/'snapshot.json'
            p.write_text(json.dumps(snap))
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(m, 'collect') as read, \
                 patch.object(e, 'enrich') as enrich, patch.object(d.SlackSender, 'post') as send:
                self.assertEqual(runtime.run(args('takken', replay=str(p), output=directory)), 0)
                read.assert_not_called(); enrich.assert_not_called(); send.assert_not_called()
            report = json.loads((Path(directory)/'report.json').read_text(encoding='utf-8'))
            self.assertEqual([r['key'] for r in report['results']], ['takken'])


if __name__ == '__main__':
    unittest.main()
