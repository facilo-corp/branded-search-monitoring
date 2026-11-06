"""Offline fault injection at the HTTP boundary; no real credentials or services."""
import base64
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import requests
import yaml
import delivery as d
import monitor as m
import receipts
import runtime
from state_store import BRANCH, FILE, GitStore, Journal, StateStoreError
from test_delivery import planned, result, Sender, authorization, NOW


def branded_pending():
    observed = result()
    observed.update(key='branded', name='Branded search')
    return planned(r=observed)


def packed(doc):
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()
    return dict(sha=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest(),
                encoding='base64', size=len(raw), content=base64.b64encode(raw).decode())


class Response:
    def __init__(self, status=200, value=None, headers=None):
        self.status_code, self.value, self.headers = status, value or {}, headers or {}

    def json(self):
        if isinstance(self.value, Exception):
            raise self.value
        return deepcopy(self.value)


class Server:
    """Git Contents CAS and immutable commit reads, independent of production code."""
    def __init__(self, doc=None):
        self.doc = deepcopy(doc or d.initial_state())
        self.commit = '1'*40
        self.history = {self.commit: deepcopy(self.doc)}
        self.calls, self.put_bodies, self.get_faults, self.put_faults = [], [], [], []
        self.saved = 0
        self.after_put = None
        self.after_pinned_read = None
        self.pending_late_write = None
        self.stale_branch = None
        self.api = {}

    def save(self, doc):
        self.saved += 1
        self.doc = deepcopy(doc)
        self.commit = hashlib.sha1(('commit-'+str(self.saved)).encode()).hexdigest()
        self.history[self.commit] = deepcopy(doc)

    def __call__(self, method, url, **kwargs):
        path = url.split('/branded-search-monitoring/', 1)[1]
        self.calls.append((method, path))
        if method == 'GET':
            if self.get_faults:
                fault = self.get_faults.pop(0)
                if isinstance(fault, Exception):
                    raise fault
                if fault is not None:
                    return fault
            if path == f'git/ref/heads/{BRANCH}':
                return Response(value={'object': {'type':'commit', 'sha':self.commit}})
            if path in self.api:
                return Response(value=self.api[path])
            if path.startswith(f'contents/{FILE}?ref='):
                ref = path.split('?ref=', 1)[1]
                doc = (self.stale_branch or self.doc) if ref == BRANCH else self.history[ref]
                response = Response(value=packed(doc))
                if ref != BRANCH and self.after_pinned_read:
                    callback, self.after_pinned_read = self.after_pinned_read, None
                    callback(self)
                return response
            raise AssertionError('Unexpected GET endpoint')
        if method != 'PUT' or path != f'contents/{FILE}':
            raise AssertionError('Unexpected external operation')
        body = deepcopy(kwargs['json'])
        self.put_bodies.append(body)
        if self.pending_late_write is not None:
            self.save(self.pending_late_write)
            self.pending_late_write = None
        if body['sha'] != packed(self.doc)['sha']:
            return Response(409)
        desired = json.loads(base64.b64decode(body['content']))
        fault = self.put_faults.pop(0) if self.put_faults else 'ok'
        if fault == 'drop_timeout':
            raise requests.Timeout('TOKEN-SECRET private@example.com')
        if fault == 'late_timeout':
            self.pending_late_write = desired
            raise requests.Timeout('TOKEN-SECRET private@example.com')
        if isinstance(fault, Response):
            return fault
        self.save(desired)
        if self.after_put:
            callback, self.after_put = self.after_put, None
            callback(self)
        if fault == 'commit_timeout':
            raise requests.Timeout('TOKEN-SECRET private@example.com')
        return Response(200, {'content':packed(self.doc), 'commit':{'sha':self.commit}})


class FaultTests(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.store = GitStore('fixture-not-a-credential', repository='facilo-corp/branded-search-monitoring', target='branded')
        self.before = packed(self.server.doc)['sha']
        self.next = {**self.server.doc, 'revision':1, 'operation_id':'test-operation'}
        self.http = self.enterContext(patch('requests.request', side_effect=self.server))
        self.sleep = self.enterContext(patch('state_store.time.sleep'))

    def write(self):
        return self.store.write(self.before, self.next)

    def test_response_lost_after_commit_does_not_write_twice(self):
        self.server.put_faults = ['commit_timeout']
        self.assertEqual(self.write(), packed(self.next)['sha'])
        self.assertEqual(len(self.server.put_bodies), 1)
        self.assertEqual(self.server.saved, 1)

    def test_transient_not_saved_retries_same_cas_once(self):
        for fault in ('drop_timeout', Response(500), Response(503)):
            with self.subTest(fault=type(fault).__name__):
                self.server = Server()
                self.http.side_effect = self.server
                self.server.put_faults = [fault]
                self.assertEqual(self.write(), packed(self.next)['sha'])
                self.assertEqual(self.server.put_bodies[0], self.server.put_bodies[1])
                self.assertEqual(self.server.saved, 1)

    def test_late_original_write_wins_cas_and_is_not_duplicated(self):
        self.server.put_faults = ['late_timeout']
        self.assertEqual(self.write(), packed(self.next)['sha'])
        self.assertEqual(len(self.server.put_bodies), 2)
        self.assertEqual(self.server.saved, 1)

    def test_unavailable_write_is_bounded_to_two_attempts(self):
        self.server.put_faults = ['drop_timeout']*3
        with self.assertRaisesRegex(ValueError, 'state_write_unconfirmed'):
            self.write()
        self.assertEqual(len(self.server.put_bodies), 2)
        self.assertEqual(self.server.saved, 0)

    def test_success_response_without_persisted_content_never_retries_put(self):
        self.server.put_faults = [Response(200)]
        with self.assertRaisesRegex(ValueError, 'state_write_unconfirmed'):
            self.write()
        self.assertEqual(len(self.server.put_bodies), 1)

    def test_permissions_missing_file_and_validation_failure_stop_immediately(self):
        for status, code in [(401,'state_access_denied'), (403,'state_access_denied'),
                             (404,'state_not_found'), (422,'state_request_rejected')]:
            with self.subTest(status=status):
                self.server = Server()
                self.http.side_effect = self.server
                self.server.put_faults = [Response(status)]
                with self.assertRaisesRegex(StateStoreError, code):
                    self.write()
                self.assertEqual(self.server.calls[-1][0], 'PUT')
                self.assertEqual(len(self.server.put_bodies), 1)

    def test_rate_limit_stops_without_followup_requests(self):
        for status, headers in [(429,{}), (403,{'retry-after':'90'}),
                                (403,{'x-ratelimit-remaining':'0'})]:
            self.server = Server()
            self.http.side_effect = self.server
            self.server.put_faults = [Response(status, headers=headers)]
            with self.assertRaisesRegex(StateStoreError, 'state_rate_limited'):
                self.write()
            self.assertEqual(self.server.calls[-1][0], 'PUT')
            self.assertEqual(len(self.server.put_bodies), 1)

    def test_get_transient_faults_recover_but_access_failure_does_not_retry(self):
        self.server.get_faults = [requests.ConnectionError('secret'), Response(502)]
        self.assertEqual(self.store.read()[0], self.before)
        self.assertEqual(len(self.server.calls), 3)
        self.server.get_faults = [Response(403)]
        with self.assertRaisesRegex(StateStoreError, 'state_access_denied'):
            self.store.read()
        self.assertEqual(len(self.server.calls), 4)

    def test_permanent_get_timeout_has_bounded_attempts(self):
        self.server.get_faults = [requests.Timeout('secret')]*10
        with self.assertRaisesRegex(StateStoreError, 'state_timeout'):
            self.store.read()
        self.assertEqual(len(self.server.calls), 3)

    def test_confirmation_get_failure_recovers_without_put(self):
        self.server.save(self.next)
        self.server.get_faults = [Response(503), requests.Timeout('secret')]
        self.assertEqual(self.store.confirm(packed(self.next)['sha'],self.next,self.before)[1], self.next)
        self.assertFalse(self.server.put_bodies)

    def test_branch_content_cache_cannot_overrule_pinned_commit(self):
        self.server.stale_branch = deepcopy(self.server.doc)
        self.assertEqual(self.write(), packed(self.next)['sha'])
        self.assertEqual(len(self.server.put_bodies), 1)
        self.assertNotIn(('GET',f'contents/{FILE}?ref={BRANCH}'), self.server.calls)

    def test_journal_stale_preflight_reads_use_verified_commit(self):
        self.server.stale_branch = deepcopy(self.server.doc)
        with Journal(self.store).exclusive() as journal:
            journal.save(deepcopy(journal.state))
        self.assertIsNone(self.server.doc['owner'])
        self.assertEqual(self.server.doc['revision'], 3)
        self.assertEqual(len(self.server.put_bodies), 3)

    def test_http500_after_slack_receipt_retries_save_without_second_send(self):
        now = datetime.now(timezone.utc)
        observation = result()
        observation.update(key='branded', name='Branded search')
        self.server = Server(d.plan(d.initial_state(), {'results':[observation]}, now))
        self.http.side_effect = self.server
        owner = 'run:101:1:'+'b'*32
        head = 'a'*40
        class ReceiptSender(Sender):
            def post(inner, channel, text, client_id):
                inner.calls.append((channel,text,client_id))
                self.server.put_faults = [Response(500)]
                return {'channel':channel,'ts':f'{now.timestamp():.6f}'}
        sender = ReceiptSender()
        env = {'GITHUB_RUN_ID':'101','GITHUB_RUN_ATTEMPT':'1',
               'GITHUB_REPOSITORY':self.store.repository,'GITHUB_SHA':head}
        with tempfile.TemporaryDirectory() as output, patch.dict(os.environ,env):
            receipt_log = receipts.ReceiptLog(output,self.store.repository,'branded',owner)
            with Journal(self.store,owner).exclusive() as journal:
                outcome = d.dispatch(journal,sender,authorization(journal.state),now,receipt_log)
            proof = json.loads(receipt_log.path.read_text(encoding='utf-8'))
        self.assertEqual(outcome['sent'],1)
        self.assertEqual(len(sender.calls),1)
        self.assertEqual(len(proof['receipts']),1)
        self.assertEqual(next(iter(self.server.doc['outbox'].values()))['status'],'sent')
        self.assertIsNone(self.server.doc['owner'])
        self.assertTrue(any(e['event']=='conditional_save_retry' for e in self.store.diagnostics))
        with Journal(self.store).exclusive() as journal:
            d.dispatch(journal,sender,authorization(journal.state),now)
        self.assertEqual(len(sender.calls),1)

    def test_twice_failed_receipt_save_is_recovered_from_proof_without_resend(self):
        now = datetime.now(timezone.utc)
        observation = result()
        observation.update(key='branded', name='Branded search')
        self.server = Server(d.plan(d.initial_state(),{'results':[observation]},now))
        self.http.side_effect = self.server
        owner, next_owner, head = 'run:101:1:'+'b'*32, 'run:202:1:'+'c'*32, 'a'*40
        class ReceiptSender(Sender):
            def post(inner,channel,text,client_id):
                inner.calls.append((channel,text,client_id))
                self.server.put_faults = [Response(500),Response(500)]
                return {'channel':channel,'ts':f'{now.timestamp():.6f}'}
        sender = ReceiptSender()
        env = {'GITHUB_RUN_ID':'101','GITHUB_RUN_ATTEMPT':'1',
               'GITHUB_REPOSITORY':self.store.repository,'GITHUB_SHA':head}
        with tempfile.TemporaryDirectory() as output, patch.dict(os.environ,env):
            receipt_log = receipts.ReceiptLog(output,self.store.repository,'branded',owner)
            with self.assertRaisesRegex(StateStoreError,'state_write_unconfirmed'):
                with Journal(self.store,owner).exclusive() as journal:
                    d.dispatch(journal,sender,authorization(journal.state),now,receipt_log)
            proof = json.loads(receipt_log.path.read_text(encoding='utf-8'))
        self.assertEqual(self.server.doc['owner'],owner)
        self.assertEqual(next(iter(self.server.doc['outbox'].values()))['status'],'sending')
        run = {'id':101,'run_attempt':1,'path':receipts.WORKFLOW,'head_branch':'main',
               'event':'schedule','status':'completed','head_sha':head,
               'repository':{'full_name':self.store.repository,'id':123},
               'head_repository':{'full_name':self.store.repository},
               'run_started_at':m.iso(now-timedelta(seconds=5)),
               'updated_at':m.iso(datetime.now(timezone.utc))}
        artifact = {'id':987,'name':receipts.artifact_name(101,1),'size_in_bytes':2000,
                    'expired':False,'created_at':m.iso(now),
                    'workflow_run':{'id':101,'head_sha':head,'head_branch':'main','repository_id':123}}
        self.server.api = {'actions/runs/101':run,'actions/runs/101/attempts/1':run,
                           'actions/runs/101/artifacts?per_page=100':{'total_count':1,'artifacts':[artifact]}}
        recovery = receipts.Recovery(self.store,lambda head:True)
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'202'}), patch.object(receipts,'download',return_value=proof):
            with Journal(self.store,next_owner).exclusive(recover=recovery) as journal:
                outcome = d.dispatch(journal,sender,authorization(journal.state),now)
        self.assertEqual(outcome['sent'],0)
        self.assertEqual(len(sender.calls),1)
        self.assertEqual(next(iter(self.server.doc['outbox'].values()))['status'],'sent')
        self.assertIsNone(self.server.doc['owner'])
        self.assertEqual(recovery.recovered[0]['run_id'],101)
        self.assertTrue(any(e['phase']=='recover_receipt' for e in self.store.diagnostics))

    def test_other_writer_after_save_stops_without_resubmitting(self):
        self.server.after_put = lambda s: s.save({**self.next, 'operation_id':'another-writer'})
        with self.assertRaisesRegex(ValueError, 'state_changed_concurrently'):
            self.write()
        self.assertEqual(len(self.server.put_bodies), 1)

    def test_branch_changes_during_history_read_prevent_write(self):
        self.server.after_pinned_read = lambda s: s.save(self.next)
        with self.assertRaisesRegex(ValueError, 'state_changed_concurrently'):
            self.write()
        self.assertFalse(self.server.put_bodies)

    def test_history_change_even_with_original_file_content_is_not_retryable(self):
        self.server.put_faults = ['commit_timeout']
        self.server.after_put = lambda s: s.save(d.initial_state())
        with self.assertRaisesRegex(ValueError, 'state_changed_concurrently'):
            self.write()
        self.assertEqual(len(self.server.put_bodies), 1)

    def test_corrupt_file_sha_and_invalid_json_are_not_accepted(self):
        response = packed(self.server.doc)
        response['sha'] = 'f'*40
        self.server.get_faults = [Response(value=response)]
        with self.assertRaisesRegex(ValueError, 'state_content_hash_mismatch'):
            self.store.read()
        self.server.get_faults = [Response(value=ValueError('secret'))]
        with self.assertRaisesRegex(StateStoreError, 'state_response_invalid'):
            self.store.read()

    def test_expired_budget_does_not_issue_a_request(self):
        self.store.deadline = 1
        with patch('state_store.time.monotonic', return_value=2):
            with self.assertRaisesRegex(StateStoreError, 'state_retry_budget_exceeded'):
                self.store.read()
        self.assertFalse(self.server.calls)

    def test_failed_lock_never_reaches_slack_or_business_processing(self):
        self.server.put_faults = ['drop_timeout']*2
        sender = Sender()
        entered = False
        with self.assertRaisesRegex(ValueError, 'state_write_unconfirmed'):
            with Journal(self.store).exclusive() as journal:
                entered = True
                d.dispatch(journal, sender, authorization(journal.state), NOW)
        self.assertFalse(entered)
        self.assertFalse(sender.calls)

    def test_receipt_write_response_loss_does_not_resend_slack(self):
        self.server = Server(branded_pending())
        self.http.side_effect = self.server
        class ReceiptSender(Sender):
            def post(inner, *args, **kwargs):
                result = super().post(*args, **kwargs)
                self.server.put_faults = ['commit_timeout']
                return result
        sender = ReceiptSender()
        with Journal(self.store).exclusive() as journal:
            outcome = d.dispatch(journal, sender, authorization(journal.state), NOW)
        self.assertEqual(outcome['sent'], 1)
        self.assertEqual(len(sender.calls), 1)
        with Journal(self.store).exclusive() as journal:
            self.assertEqual(d.dispatch(journal,sender,authorization(journal.state),NOW)['sent'], 0)
        self.assertEqual(len(sender.calls), 1)

    def test_unknown_existing_slack_send_remains_held(self):
        doc = branded_pending()
        doc['owner'] = 'run:10:1:'+'a'*32
        next(iter(doc['outbox'].values()))['status'] = 'needs_review'
        self.server = Server(doc)
        self.http.side_effect = self.server
        with self.assertRaisesRegex(ValueError, 'monitor_delivery_requires_review'):
            with Journal(self.store).exclusive():
                self.fail('must not reach notification processing')
        self.assertFalse(self.server.put_bodies)

    def test_failed_receipt_persistence_preserves_unknown_send_and_never_resends(self):
        self.server = Server(branded_pending())
        self.http.side_effect = self.server
        class ReceiptSender(Sender):
            def post(inner, *args, **kwargs):
                result = super().post(*args, **kwargs)
                self.server.put_faults = ['drop_timeout']*2
                return result
        sender = ReceiptSender()
        with self.assertRaisesRegex(ValueError, 'state_write_unconfirmed'):
            with Journal(self.store).exclusive() as journal:
                d.dispatch(journal,sender,authorization(journal.state),NOW)
        self.assertEqual(len(sender.calls),1)
        self.assertEqual(next(iter(self.server.doc['outbox'].values()))['status'],'sending')
        with self.assertRaisesRegex(ValueError, 'monitor_delivery_requires_review'):
            with Journal(self.store).exclusive() as journal:
                d.dispatch(journal,sender,authorization(journal.state),NOW)
        self.assertEqual(len(sender.calls),1)

    def test_state_changes_before_conditional_retry_prevent_second_put(self):
        original = self.store.current
        count = 0
        def current():
            nonlocal count
            count += 1
            # Pre-write snapshot + four reconciliation snapshots, then retry preflight.
            if count == 6:
                self.server.save({**self.next, 'operation_id':'another-writer'})
            return original()
        self.server.put_faults = ['drop_timeout']
        with patch.object(self.store,'current',side_effect=current):
            with self.assertRaisesRegex(ValueError,'state_changed_concurrently'):
                self.write()
        self.assertEqual(len(self.server.put_bodies),1)

    def test_history_unavailable_after_write_never_allows_conditional_put(self):
        original = self.store.current
        count = 0
        def current():
            nonlocal count
            count += 1
            if count > 1:
                raise StateStoreError('state_connection_error')
            return original()
        self.server.put_faults = ['drop_timeout']
        with patch.object(self.store,'current',side_effect=current):
            with self.assertRaisesRegex(ValueError,'state_write_unconfirmed'):
                self.write()
        self.assertEqual(len(self.server.put_bodies),1)

    def test_diagnostics_survive_failure_and_never_include_sensitive_values(self):
        self.server.put_faults = ['drop_timeout']*2
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                with runtime.record_store_diagnostics(self.store, temp):
                    with Journal(self.store).exclusive():
                        pass
            text = (Path(temp)/'state-diagnostics.json').read_text(encoding='utf-8')
            self.assertIn('state_timeout', text)
            self.assertIn('acquire_lock', text)
            for secret in ['TOKEN-SECRET','private@example.com','fixture-not-a-credential',
                           'Authorization','https://','"content":','operation_id']:
                self.assertNotIn(secret, text)

    def test_secondary_rate_limit_without_headers_is_identified_without_body_leak(self):
        self.server.get_faults = [Response(403, {'message':'secondary rate limit TOKEN-SECRET'})]
        with self.assertRaisesRegex(StateStoreError, 'state_rate_limited'):
            self.store.read()
        self.assertEqual(len(self.server.calls), 1)
        self.assertNotIn('TOKEN-SECRET', json.dumps(self.store.diagnostics))

    def test_exception_diagnostic_cannot_accept_unreviewed_text(self):
        error = StateStoreError('private@example.com TOKEN-SECRET', method='https://secret', status='secret')
        self.assertEqual(error.diagnostic, {'code':'state_request_rejected'})

    def test_failure_artifact_and_fixed_reason_are_available(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch('sys.argv',['runtime.py','--target','branded','--output',temp]), \
             patch.object(runtime,'run',side_effect=StateStoreError('state_access_denied',method='PUT',status=403)), \
             patch.dict('os.environ', {'GITHUB_STEP_SUMMARY':''}), redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.main(), 1)
            failure = json.loads((Path(temp)/'failure.json').read_text(encoding='utf-8'))
            self.assertEqual(failure['reason'], 'state_access_denied')
            self.assertEqual(failure['diagnostic']['http_status'], 403)
        workflow = yaml.safe_load((Path(__file__).parents[1]/'.github/workflows/automation-monitor.yml').read_text())
        upload = next(s for s in workflow['jobs']['monitor']['steps'] if 'monitor-result/failure.json' in s.get('with',{}).get('path',''))
        self.assertEqual(upload['if'], 'always()')
        self.assertIn('monitor-result/failure.json', upload['with']['path'].splitlines())
        self.assertIn('monitor-result/state-diagnostics.json', upload['with']['path'].splitlines())


if __name__ == '__main__':
    unittest.main()
