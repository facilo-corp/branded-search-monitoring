"""Regression coverage for ref drift and provably pre-send failures, fully offline."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import delivery as d
import monitor as m
import runtime
from state_store import GitStore, Journal, MemoryStore, StateStoreError, BRANCH
from test_delivery import result, Sender, authorization, NOW
from test_state_storage_faults import Server, Response, packed

TARGET = 'branded'


def pending(signature='new-case'):
    observed=result(signature=signature)
    observed.update(key=TARGET, name=TARGET)
    return d.plan(d.initial_state(), {'results':[observed]}, NOW)


def with_history():
    memory=MemoryStore(pending('prior-case'))
    with Journal(memory).exclusive() as journal:
        d.dispatch(journal,Sender(),authorization(journal.state),NOW)
    observed=result(signature='new-case')
    observed.update(key=TARGET,name=TARGET)
    return d.plan(memory.doc,{'results':[observed]},NOW)


class ScriptedServer(Server):
    def __init__(self,doc):
        super().__init__(doc)
        self.ref_script=[]
        self.after_save=None
        self.before_put=None

    def __call__(self,method,url,**kwargs):
        if method=='GET' and url.endswith(f'git/ref/heads/{BRANCH}') and self.ref_script:
            self.calls.append((method,'scripted-ref'))
            return Response(value={'object':{'type':'commit','sha':self.ref_script.pop(0)}})
        before=self.commit
        if method=='PUT' and self.before_put:
            self.before_put(self,kwargs['json'])
        response=super().__call__(method,url,**kwargs)
        if method=='PUT' and response.status_code==200 and self.after_save:
            self.after_save(self,before,self.commit)
        return response


class ReadConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.initial=with_history()
        self.server=ScriptedServer(self.initial)
        self.store=GitStore('offline-credential',repository=m.CONFIG[TARGET]['repo'],target=TARGET)
        self.http=self.enterContext(patch('requests.request',side_effect=lambda *a,**k:self.server(*a,**k)))
        self.enterContext(patch('state_store.time.sleep'))
        self.sender=Sender()

    def active_row(self):
        return next(r for r in self.server.doc['outbox'].values() if r['signature']!=next(iter(self.initial['outbox'].values()))['signature'])

    def dispatch(self):
        with Journal(self.store).exclusive() as journal:
            return d.dispatch(journal,self.sender,authorization(journal.state),NOW)

    def assert_history(self):
        for eid,row in self.initial['outbox'].items():
            if row['status']=='sent':
                self.assertEqual(self.server.doc['outbox'][eid],row)

    def drift_on_intent(self, sequences):
        def after(server,old,new):
            if any(r['status']=='sending' for r in server.doc['outbox'].values()):
                server.after_save=None
                server.ref_script=[old if label=='old' else new for label in sequences]
        self.server.after_save=after

    def test_old_then_new_ref_after_every_successful_save_recovers(self):
        self.server.after_save=lambda s,old,new:setattr(s,'ref_script',[old,new])
        self.assertEqual(self.dispatch()['sent'],1)
        self.assertEqual(len(self.sender.calls),1)
        self.assertEqual(len(self.server.put_bodies),4)
        self.assertIsNone(self.server.doc['owner'])
        self.assertTrue(any(e['event']=='branch_head_moved' for e in self.store.diagnostics))
        self.assert_history()

    def test_stable_old_ref_then_new_ref_confirms_without_another_put(self):
        self.drift_on_intent(['old','old'])
        self.assertEqual(self.dispatch()['sent'],1)
        self.assertEqual(len(self.sender.calls),1)
        self.assertEqual(len(self.server.put_bodies),4)

    def test_new_then_old_ref_is_also_read_again_before_sending(self):
        self.drift_on_intent(['new','old'])
        self.assertEqual(self.dispatch()['sent'],1)
        self.assertEqual(len(self.sender.calls),1)

    def test_exhausted_initial_readback_restores_known_unsent_and_next_run_sends_once(self):
        self.drift_on_intent(['old','new']*3)
        with self.assertRaisesRegex(ValueError,'monitor_delivery_not_started'):
            self.dispatch()
        self.assertEqual(self.sender.calls,[])
        self.assertEqual(self.active_row()['status'],'pending')
        self.assertNotIn('attempted_at',self.active_row())
        self.assertIsNone(self.server.doc['owner'])
        self.assertTrue(any(e['event']=='unsent_intent_restored' for e in self.store.diagnostics))
        self.assert_history()
        self.assertEqual(self.dispatch()['sent'],1)
        self.assertEqual(self.dispatch()['sent'],0)
        self.assertEqual(len(self.sender.calls),1)

    def test_unstable_reads_are_bounded_and_do_not_fabricate_restoration(self):
        self.drift_on_intent(['old','new']*20)
        with self.assertRaisesRegex(ValueError,'monitor_delivery_not_started'):
            self.dispatch()
        self.assertEqual(self.sender.calls,[])
        self.assertEqual(self.active_row()['status'],'sending')
        self.assertIsNotNone(self.server.doc['owner'])
        self.assertEqual(len(self.server.put_bodies),2)  # lock, intent only
        self.assertEqual(sum(e['event']=='branch_head_moved' for e in self.store.diagnostics),6)

    def test_real_writer_after_intent_is_never_overwritten(self):
        other={}
        def after(server,old,new):
            if any(r['status']=='sending' for r in server.doc['outbox'].values()):
                server.after_save=None
                server.save({**server.doc,'operation_id':'other-writer','revision':server.doc['revision']+1})
                other.update(deepcopy(server.doc))
        self.server.after_save=after
        with self.assertRaisesRegex(ValueError,'monitor_delivery_not_started'):
            self.dispatch()
        self.assertEqual(self.server.doc,other)
        self.assertEqual(len(self.server.put_bodies),2)
        self.assertEqual(self.sender.calls,[])

    def test_cached_branch_contents_do_not_override_committed_state(self):
        self.server.stale_branch=deepcopy(self.server.doc)
        self.assertEqual(self.dispatch()['sent'],1)
        self.assertEqual(len(self.sender.calls),1)
        self.assertIsNone(self.server.doc['owner'])

    def test_preflight_read_failure_after_post_preserves_unknown_send(self):
        owner=self
        class AfterPost(Sender):
            def post(self,*args,**kwargs):
                receipt=super().post(*args,**kwargs)
                owner.server.get_faults=[Response(500)]*3
                return receipt
        self.sender=AfterPost()
        with self.assertRaisesRegex(ValueError,'state_service_unavailable'):
            self.dispatch()
        self.assertEqual(len(self.sender.calls),1)
        self.assertEqual(self.active_row()['status'],'sending')
        self.assertIsNotNone(self.server.doc['owner'])
        self.assertFalse(any(e['event']=='unsent_intent_restored' for e in self.store.diagnostics))
        with self.assertRaisesRegex(ValueError,'monitor_delivery_requires_review'):
            self.dispatch()
        self.assertEqual(len(self.sender.calls),1)

    def test_ref_drift_after_receipt_save_does_not_resend(self):
        def after(server,old,new):
            if len([r for r in server.doc['outbox'].values() if r['status']=='sent'])==2:
                server.after_save=None
                server.ref_script=[old,new]
        self.server.after_save=after
        self.assertEqual(self.dispatch()['sent'],1)
        self.assertEqual(self.dispatch()['sent'],0)
        self.assertEqual(len(self.sender.calls),1)

    def test_unknown_sender_failure_remains_review_required(self):
        self.sender=Sender(failure=True)
        self.assertEqual(self.dispatch()['needs_review'],1)
        self.assertEqual(self.active_row()['status'],'needs_review')
        self.assertEqual(self.dispatch()['sent'],0)
        self.assertEqual(len(self.sender.calls),1)
        self.assertFalse(any(e['event']=='unsent_intent_restored' for e in self.store.diagnostics))

    def drop_intent(self):
        def before(server,body):
            import base64
            doc=json.loads(base64.b64decode(body['content']))
            if any(r['status']=='sending' for r in doc['outbox'].values()):
                server.before_put=None
                server.put_faults=['drop_timeout']*2
        self.server.before_put=before

    def test_absent_intent_is_sealed_so_a_late_original_put_cannot_revive_it(self):
        self.drop_intent()
        with self.assertRaisesRegex(ValueError,'monitor_delivery_not_started'):
            self.dispatch()
        late=deepcopy(self.server.put_bodies[1])
        self.assertEqual(self.active_row()['status'],'pending')
        self.assertIsNone(self.server.doc['owner'])
        self.assertEqual(self.sender.calls,[])
        before=deepcopy(self.server.doc)
        response=self.server('PUT',f'https://api.github.com/repos/{self.store.repository}/contents/alerts.json',json=late)
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.server.doc,before)
        self.assertEqual(self.dispatch()['sent'],1)
        self.assertEqual(len(self.sender.calls),1)

    def test_late_original_winning_cas_blocks_restoration_without_sending(self):
        self.drop_intent()
        original=self.store.current
        count=0
        def current():
            nonlocal count
            value=original()
            # Restoration is explicitly marked, so inject a late intent only
            # after its fixed read and before the restore write's CAS preflight.
            if self.store.phase=='restore_unsent_intent':
                count+=1
                if count==2:
                    import base64
                    self.server.pending_late_write=json.loads(base64.b64decode(self.server.put_bodies[1]['content']))
            return value
        with patch.object(self.store,'current',side_effect=current):
            with self.assertRaisesRegex(ValueError,'monitor_delivery_not_started'):
                self.dispatch()
        self.assertEqual(self.active_row()['status'],'sending')
        self.assertIsNotNone(self.server.doc['owner'])
        self.assertEqual(self.sender.calls,[])

    def test_old_unknown_sending_row_cannot_use_live_presend_restoration(self):
        doc=pending()
        eid=next(iter(doc['outbox']))
        doc['outbox'][eid]['status']='sending'
        doc['owner']='existing-owner'
        journal=Journal(MemoryStore(doc),owner='existing-owner')
        journal.sha,journal.state=journal.store.read()
        with self.assertRaisesRegex(ValueError,'delivery_intent_not_pending'):
            journal.prepare_delivery(eid,m.iso(NOW))
        self.assertEqual(journal.store.doc,doc)

    def test_ref_never_stable_prevents_any_write(self):
        old=self.server.commit
        self.server.save({**self.server.doc,'operation_id':'other'})
        self.server.ref_script=[old,self.server.commit]*5
        with self.assertRaisesRegex(ValueError,'state_readback_unconfirmed'):
            self.store.current()
        self.assertEqual(self.server.put_bodies,[])
        self.assertEqual(sum(e['event']=='branch_head_moved' for e in self.store.diagnostics),3)

    def test_runtime_explains_known_no_send_separately_from_unknown_delivery(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch('sys.argv',['runtime.py','--target',TARGET,'--output',temp]), \
             patch.object(runtime,'run',side_effect=ValueError('monitor_delivery_not_started')), \
             patch.dict('os.environ',{'GITHUB_STEP_SUMMARY':''}),redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.main(),1)
            failure=json.loads((Path(temp)/'failure.json').read_text(encoding='utf-8'))
        self.assertEqual(failure['reason'],'monitor_delivery_not_started')
        self.assertIn('通知は送信していません',failure['cause'])


if __name__=='__main__':
    unittest.main()
