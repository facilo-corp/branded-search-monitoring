import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import delivery as d
import monitor as m
import runtime
from state_store import GitStore, Journal, execution_owner
from test_monitor import evidence, run, NOW
from test_delivery import planned, Sender, authorization
import test_delivery


def payload(doc):
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()
    sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    return {'encoding':'base64','size':len(raw),'content':base64.b64encode(raw).decode(),'sha':sha}


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.old=d.initial_state()
        self.new={**self.old,'revision':1,'operation_id':'new'}
        self.previous=payload(self.old)['sha']

    def test_old_old_new_read_does_not_repeat_put(self):
        with patch.object(GitStore,'request',side_effect=[{},payload(self.old),payload(self.old),payload(self.new)]) as req, patch('state_store.time.sleep'):
            self.assertEqual(GitStore('fixture').write(self.previous,self.new),payload(self.new)['sha'])
            self.assertEqual([c.args[0] for c in req.call_args_list],['PUT','GET','GET','GET'])

    def test_lost_write_response_is_reconciled_by_read_only(self):
        with patch.object(GitStore,'request',side_effect=[ValueError('state_api_unavailable'),payload(self.new)]) as req:
            self.assertEqual(GitStore('fixture').write(self.previous,self.new),payload(self.new)['sha'])
            self.assertEqual(req.call_count,2)

    def test_other_writer_fails_immediately_without_retrying_write(self):
        other={**self.new,'operation_id':'other-writer'}
        with patch.object(GitStore,'request',side_effect=[{},payload(other)]) as req:
            with self.assertRaisesRegex(ValueError,'state_changed_concurrently'):
                GitStore('fixture').write(self.previous,self.new)
            self.assertEqual(req.call_count,2)

    def test_permanently_old_read_is_bounded_and_stays_failed(self):
        with patch.object(GitStore,'request',side_effect=[{}]+[payload(self.old)]*4) as req, patch('state_store.time.sleep'):
            with self.assertRaisesRegex(ValueError,'state_write_unconfirmed'):
                GitStore('fixture').write(self.previous,self.new)
            self.assertEqual(req.call_count,5)

    def test_read_failure_retries_get_only(self):
        with patch.object(GitStore,'request',side_effect=[{},ValueError('state_api_unavailable'),payload(self.new)]) as req, patch('state_store.time.sleep'):
            GitStore('fixture').write(self.previous,self.new)
            self.assertEqual([c.args[0] for c in req.call_args_list],['PUT','GET','GET'])

    def test_malformed_state_cannot_be_accepted_after_retry(self):
        wrong=payload(self.new);wrong['encoding']='other'
        with patch.object(GitStore,'request',side_effect=[{},wrong]) as req:
            with self.assertRaisesRegex(ValueError,'state_content_invalid'):
                GitStore('fixture').write(self.previous,self.new)
            self.assertEqual(req.call_count,2)

    def test_second_readback_can_tolerate_old_version(self):
        with patch.object(GitStore,'request',side_effect=[payload(self.old),payload(self.new)]),patch('state_store.time.sleep'):
            self.assertEqual(GitStore('fixture').confirm(payload(self.new)['sha'],self.new,self.previous)[1],self.new)


class ResumableStore(GitStore):
    def __init__(self,doc,finished=True):
        self.doc=deepcopy(doc);self.finished=finished;self.puts=0
    def read(self): return payload(self.doc)['sha'],deepcopy(self.doc)
    def write(self,previous,doc):
        assert previous==self.read()[0]
        self.puts+=1;self.doc=deepcopy(doc)
        return self.read()[0]
    def previous_owner_finished(self,owner):return self.finished


class OwnerRecoveryTests(unittest.TestCase):
    def test_run_identity_is_saved_in_owner(self):
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'201','GITHUB_RUN_ATTEMPT':'1'}):
            self.assertRegex(execution_owner(),r'^run:201:1:[0-9a-f]{32}$')

    def test_completed_run_and_attempt_are_both_required(self):
        owner='run:101:2:'+'a'*32
        item={'id':101,'path':'.github/workflows/automation-monitor.yml','head_branch':'main','event':'schedule','status':'completed','run_attempt':2}
        with patch.object(GitStore,'request',side_effect=[item,item]) as req:
            self.assertTrue(GitStore('fixture').previous_owner_finished(owner))
            self.assertIn('/attempts/2',req.call_args_list[1].args[1])
        for first,second in [({**item,'status':'in_progress'},item),(item,{**item,'status':'in_progress'})]:
            with patch.object(GitStore,'request',side_effect=[first,second]):
                self.assertFalse(GitStore('fixture').previous_owner_finished(owner))

    def test_legacy_and_same_run_lock_are_not_recovered(self):
        with patch.object(GitStore,'request') as req,patch.dict(os.environ,{'GITHUB_RUN_ID':'101'}):
            self.assertFalse(GitStore('fixture').previous_owner_finished('legacy-uuid'))
            self.assertFalse(GitStore('fixture').previous_owner_finished('run:101:1:'+'a'*32))
            req.assert_not_called()

    def test_wrong_workflow_or_attempt_stays_locked(self):
        item={'id':101,'path':'.github/workflows/automation-monitor.yml','head_branch':'main','event':'schedule','status':'completed','run_attempt':2}
        for bad in [{**item,'path':'.github/workflows/other.yml'},{**item,'run_attempt':3}]:
            with patch.object(GitStore,'request',side_effect=[item,bad]):
                with self.assertRaisesRegex(ValueError,'monitor_previous_run_unverified'):
                    GitStore('fixture').previous_owner_finished('run:101:2:'+'a'*32)

    def test_finished_owner_releases_without_touching_outbox(self):
        doc=planned();doc['owner']='finished-owner';saved=deepcopy(doc['outbox'])
        store=ResumableStore(doc)
        with Journal(store).exclusive(): pass
        self.assertIsNone(store.doc['owner']);self.assertEqual(store.doc['outbox'],saved)

    def test_active_owner_and_unknown_send_never_mutate(self):
        for status,finished in [('pending',False),('sending',True),('needs_review',True)]:
            doc=planned();doc['owner']='held';next(iter(doc['outbox'].values()))['status']=status
            store=ResumableStore(doc,finished)
            with self.assertRaises(ValueError):
                with Journal(store).exclusive(): pass
            self.assertEqual(store.puts,0);self.assertEqual(store.doc,doc)

    def test_completed_owner_recovery_never_resends_confirmed_message(self):
        doc,_=test_delivery.OutboxTests().sent();doc['owner']='finished-owner'
        saved=deepcopy(doc['outbox']);store=ResumableStore(doc);sender=Sender()
        with Journal(store).exclusive() as journal:
            outcome=d.dispatch(journal,sender,authorization(journal.state),NOW)
        self.assertEqual(outcome['sent'],0);self.assertFalse(sender.calls)
        self.assertEqual(store.doc['outbox'],saved)


class DiagnosticTests(unittest.TestCase):
    def test_known_diagnostic_and_unknown_exception_are_sanitized(self):
        for message,expected in [('monitor_lock_requires_review','monitor_lock_requires_review'),('private@example.com SECRET','monitor_requires_review_no_automatic_retry')]:
            with tempfile.TemporaryDirectory() as tmp:
                out=Path(tmp)/'result';summary=Path(tmp)/'summary.md'
                with patch('sys.argv',['runtime.py','--target','branded','--output',str(out)]),patch.object(runtime,'run',side_effect=ValueError(message)),patch.dict(os.environ,{'GITHUB_STEP_SUMMARY':str(summary)}),redirect_stdout(io.StringIO()) as log:
                    self.assertEqual(runtime.main(),1)
                data=json.loads((out/'failure.json').read_text(encoding='utf-8'))
                self.assertEqual(data['reason'],expected)
                self.assertFalse(data['business_recovery_verified'])
                self.assertNotIn('SECRET',log.getvalue()+summary.read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
