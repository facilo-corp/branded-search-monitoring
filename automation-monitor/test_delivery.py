import copy
import json
import os
from datetime import timedelta
import unittest
from unittest.mock import patch

import delivery as d
import monitor as m
from state_store import GitStore, MemoryStore, Journal
from runtime import heartbeat_status

NOW = m.parse_time("2026-10-11T04:00:00Z")


def result(state="action_required", signature="case1"):
    r = m.base_result("takken", NOW)
    r.update(state=state, condition="business_hold" if state == "action_required" else "registration_passed",
             title="照合待ち", impact="申込み2件を保留しています", done="処理は完了、保留を継続",
             next_action="申込情報と既存会員を照合する", signature_data={"case": signature})
    return r


def planned(state=None, r=None, now=NOW):
    return d.plan(state or d.initial_state(), {"results": [r or result()]}, now)


def authorization(state):
    return {"approved": True, "mode": "exact_messages", "channel": m.CHANNEL_ID,
            "approval_reference": "unit-test-only-no-network", "messages": [
                {"id": r["id"], "text_sha256": r["text_sha256"]} for r in state["outbox"].values()]}


class Sender:
    def __init__(self, failure=False): self.calls=[]; self.failure=failure
    def post(self, channel, text, client_id):
        self.calls.append((channel, text, client_id))
        if self.failure: raise TimeoutError("private response must not leak")
        return {"channel": channel, "ts": "1791684000.000001"}


class OutboxTests(unittest.TestCase):
    def sent(self):
        store=MemoryStore(planned()); sender=Sender()
        with Journal(store).exclusive() as journal:
            d.dispatch(journal,sender,authorization(journal.state),NOW)
        return store.read()[1],sender

    def test_repeated_poll_does_not_duplicate_pending(self):
        state=planned()
        for minutes in (15,30,60,1500):state=planned(state,now=NOW+timedelta(minutes=minutes))
        self.assertEqual(len(state['outbox']),1)

    def test_changed_input_gets_new_case_and_old_pending_is_superseded(self):
        state=planned(planned(),result(signature='new-input'))
        self.assertEqual(sorted(r['status'] for r in state['outbox'].values()),['pending','superseded'])

    def test_no_healthy_or_normal_running_notifications(self):
        self.assertFalse(planned(r=result('healthy'))['outbox'])
        self.assertFalse(planned(r=result('watching'))['outbox'])

    def test_recovery_only_after_confirmed_problem_delivery(self):
        self.assertFalse(any(r['event']=='recovery' for r in planned(planned(),result('healthy'))['outbox'].values()))
        sent,_=self.sent()
        recovered=planned(sent,result('healthy'))
        self.assertEqual(sum(r['event']=='recovery' for r in recovered['outbox'].values()),1)
        self.assertEqual(len(planned(recovered,result('healthy'))['outbox']),2)

    def test_reminder_24h_from_confirmed_sending_not_each_poll(self):
        state,_=self.sent()
        self.assertEqual(len(planned(state,now=NOW+timedelta(hours=23))['outbox']),1)
        once=planned(state,now=NOW+timedelta(hours=24))
        self.assertEqual(len(once['outbox']),2)
        self.assertEqual(len(planned(once,now=NOW+timedelta(hours=25))['outbox']),2)

    def test_acknowledgement_suppresses_reminder_but_not_changed_input(self):
        state,_=self.sent()
        state=d.acknowledge(state,'takken',state['workflows']['takken']['signature'])
        self.assertEqual(len(planned(state,now=NOW+timedelta(hours=25))['outbox']),1)
        self.assertEqual(len(planned(state,result(signature='changed'))['outbox']),2)
        with self.assertRaisesRegex(ValueError,'version_changed'):d.acknowledge(state,'takken','old')

    def test_waiting_run_does_not_clear_unresolved_issue(self):
        state=planned()
        waiting=planned(state,result('watching'))
        self.assertEqual(waiting['workflows'],state['workflows'])

    def test_acknowledgement_cancels_pending_reminder(self):
        state,_=self.sent()
        queued=planned(state,now=NOW+timedelta(hours=25))
        ack=d.acknowledge(queued,'takken',queued['workflows']['takken']['signature'])
        self.assertFalse(any(r['status']=='pending' for r in ack['outbox'].values()))


class DeliverySafety(unittest.TestCase):
    def test_one_time_test_is_isolated_and_cannot_be_resent(self):
        doc=d.enqueue_test(planned(),NOW)
        test=next(r for r in doc['outbox'].values() if r['event']=='test')
        approval={**authorization(doc),'messages':[{'id':test['id'],'text_sha256':test['text_sha256']}]}
        store=MemoryStore(doc);sender=Sender()
        for _ in range(2):
            with Journal(store).exclusive() as j:
                j.save(d.enqueue_test(j.state,NOW))
                outcome=d.dispatch(j,sender,approval,NOW)
                self.assertEqual(outcome['approval_blocked'],0)
        self.assertEqual(len(sender.calls),1)
        self.assertEqual(sender.calls[0][1],d.TEST_MESSAGE)
        self.assertTrue(any(r['key']=='takken' and r['status']=='pending' for r in store.doc['outbox'].values()))

    def test_test_content_cannot_be_changed(self):
        state=d.enqueue_test(d.initial_state(),NOW)
        row=next(iter(state['outbox'].values()));row['text']='different';row['text_sha256']=d.digest(row['text'])
        with self.assertRaisesRegex(ValueError,'test_message_scope_invalid'):d.validate(state)

    def test_no_approval_no_sender_call(self):
        sender=Sender()
        with Journal(MemoryStore(planned())).exclusive() as j:
            for approval in [None,{'approved':False}, {**authorization(j.state),'channel':'other'}]:
                d.dispatch(j,sender,approval,NOW)
        self.assertFalse(sender.calls)

    def test_exact_text_change_revokes_approval(self):
        doc=planned(); approval=authorization(doc)
        row=next(iter(doc['outbox'].values())); row['text']+='changed';row['text_sha256']=d.digest(row['text'])
        self.assertFalse(d.approved(row,approval,NOW))

    def test_confirmed_send_and_restart_never_resend(self):
        store=MemoryStore(planned());sender=Sender()
        for _ in range(2):
            with Journal(store).exclusive() as j:d.dispatch(j,sender,authorization(j.state),NOW)
        self.assertEqual(len(sender.calls),1)

    def test_lost_slack_response_never_retries(self):
        store=MemoryStore(planned());sender=Sender(True)
        for _ in range(2):
            with Journal(store).exclusive() as j:d.dispatch(j,sender,authorization(j.state),NOW)
        self.assertEqual(len(sender.calls),1)
        row=next(iter(store.doc['outbox'].values()))
        self.assertEqual(row['status'],'needs_review')
        self.assertNotIn('private response',json.dumps(store.doc))

    def test_intent_save_failure_prevents_network_call(self):
        store=MemoryStore(planned());sender=Sender()
        with Journal(store).exclusive() as j:
            original=j.save
            def stop(doc):raise ValueError('intent_write_failed')
            with patch.object(j,'save',side_effect=stop):
                with self.assertRaises(ValueError):d.dispatch(j,sender,authorization(j.state),NOW)
        self.assertFalse(sender.calls)

    def test_receipt_save_failure_leaves_intent_for_review(self):
        store=MemoryStore(planned());sender=Sender();journal=Journal(store)
        with self.assertRaises(ValueError):
            with journal.exclusive() as j:
                original=store.write
                def fail(sha,doc):
                    if any(r['status']=='sent' for r in doc['outbox'].values()):raise ValueError('disk_failed')
                    return original(sha,doc)
                with patch.object(store,'write',side_effect=fail):d.dispatch(j,sender,authorization(j.state),NOW)
        self.assertEqual(len(sender.calls),1)
        self.assertIsNotNone(store.doc['owner'])
        held=planned(store.doc)
        self.assertEqual(next(iter(held['outbox'].values()))['status'],'needs_review')

    def test_interrupted_lock_and_missing_state_do_not_initialize_implicitly(self):
        doc=planned();doc['owner']='interrupted'
        with self.assertRaisesRegex(ValueError,'lock_requires_review'):
            with Journal(MemoryStore(doc)).exclusive():pass
        with patch.object(GitStore,'request',side_effect=ValueError('not_found')):
            with self.assertRaises(ValueError):GitStore('fake').read()

    def test_concurrent_journals_cannot_both_own_state(self):
        store=MemoryStore(planned())
        with Journal(store).exclusive():
            with self.assertRaisesRegex(ValueError,'lock_requires_review'):
                with Journal(store).exclusive():pass

    def test_unknown_delivery_cannot_be_reset_to_pending(self):
        doc=planned();next(iter(doc['outbox'].values()))['status']='needs_review'
        with Journal(MemoryStore(doc)).exclusive() as j:
            unsafe=copy.deepcopy(j.state);next(iter(unsafe['outbox'].values()))['status']='pending'
            with self.assertRaisesRegex(ValueError,'cannot_be_rewound'):j.save(unsafe)

    def test_unknown_write_response_uses_readback_not_retry(self):
        doc=planned();calls=[];store=GitStore('fake')
        def req(method,suffix,body=None):calls.append(method);raise TimeoutError()
        with patch.object(store,'request',side_effect=req),patch.object(store,'read',return_value=('new',doc)):
            self.assertEqual(store.write('old',doc),'new')
        self.assertEqual(calls,['PUT'])

    def test_recurring_mode_requires_specific_code_and_exception(self):
        row=next(iter(planned()['outbox'].values()))
        policy={"approved":True,"channel":m.CHANNEL_ID,"approval_reference":"fixture-only",
                "mode":"recurring_template_exception","exception_scope":"only_these_marketing_alerts",
                "template_version":d.TEMPLATE_VERSION,"code_sha256":d.code_fingerprint(),
                "not_before":m.iso(NOW),"expires_at":m.iso(NOW+timedelta(days=90))}
        self.assertTrue(d.approved(row,policy,NOW))
        for k,v in [('code_sha256','changed'),('exception_scope','all-slack'),('approved',False)]:
            self.assertFalse(d.approved(row,{**policy,k:v},NOW))
        self.assertFalse(d.approved(row,policy,NOW+timedelta(days=91)))

    def test_transport_is_single_post_and_does_not_redirect(self):
        fake=type('Response',(),{'status_code':200,'json':lambda self:{'ok':True,'channel':m.CHANNEL_ID,'ts':'1.000001'}})()
        with patch.dict(os.environ,{'ALERT_DELIVERY_ENABLED':'true'}),patch('requests.post',return_value=fake) as post:
            d.SlackSender('test-token').post(m.CHANNEL_ID,'fixture text','fixture-id')
            self.assertEqual(post.call_count,1)
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
        with patch.dict(os.environ,{'ALERT_DELIVERY_ENABLED':'false'}):
            with self.assertRaisesRegex(ValueError,'disabled'):d.SlackSender('fake')

    def test_heartbeat_detects_missing_stale_and_future(self):
        self.assertEqual(heartbeat_status({},NOW),'missing')
        self.assertEqual(heartbeat_status({'heartbeat':m.iso(NOW)},NOW),'current')
        self.assertEqual(heartbeat_status({'heartbeat':m.iso(NOW)},NOW+timedelta(minutes=61)),'stale')
        self.assertEqual(heartbeat_status({'heartbeat':m.iso(NOW+timedelta(hours=1))},NOW),'invalid')


if __name__=='__main__':unittest.main()
