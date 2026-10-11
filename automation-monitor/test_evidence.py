import copy
import json
import unittest
from unittest.mock import patch

import evidence as e
import monitor as m
from test_monitor import run, evidence, summary, NOW


class EvidenceTests(unittest.TestCase):
    def fixture(self):
        src=[['イベントID','種別','メール'],['event1','target','private@example.test'],['event2','other','second@example.test']]
        cfg={'spreadsheet_id':e.TARGET,'sheet_name':e.TARGET_TAB,'filter':{'種別':'target'},'columns':['イベントID','メール']}
        return src,[['イベントID','メール'],['event1','private@example.test']],cfg

    def test_exact_selection_comparison_without_pii_in_evidence(self):
        result=e.compare_distribution(*self.fixture())
        self.assertEqual(result['source_rows'],2)
        self.assertEqual(result['shared_rows'],1)
        self.assertNotIn('example',json.dumps(result))

    def test_missing_extra_or_changed_shared_rows_fail(self):
        src,target,cfg=self.fixture()
        for changed in (target[:1],target+target[1:],target+[ ['event2','leak'] ]):
            with self.assertRaises(m.ReadError):e.compare_distribution(src,changed,cfg)

    def test_missing_filter_or_selected_column_never_broadens_scope(self):
        src,target,cfg=self.fixture()
        for changed in ({**cfg,'filter':{'absent':'target'}},{**cfg,'columns':['absent']}):
            with self.assertRaises(m.ReadError):e.compare_distribution(src,target,changed)

    def test_stable_review_ids_exclude_times_and_raw_person_fields(self):
        s=summary({'name_email_conflict':2})
        s['review_registry']={'open':2,'cases':[{'case_id':c*64,'input_version':'b'*64,'status':'open','reason':'name_email_conflict','email':'private@example.test'} for c in ('a','c')]}
        parsed=m.extract_summary(json.dumps(s))
        self.assertEqual(len(parsed['review_cases']),2)
        self.assertNotIn('example',json.dumps(parsed))
        ev=evidence('takken',[run('takken',summary=parsed)])
        one=m.assess('takken',ev,NOW)
        ev['runs'][0]['id']=999
        two=m.assess('takken',ev,NOW)
        self.assertEqual(one['signature_data'],two['signature_data'])

    def test_invalid_registry_never_reports_healthy(self):
        s=summary();s['review_registry']={'open':1,'cases':[]}
        self.assertTrue(m.extract_summary(json.dumps(s))['invalid_counts'])

    def test_runtime_failure_is_not_a_normal_application_hold(self):
        s=summary({'browser_operation_unconfirmed_review':1})
        r=m.assess('takken',evidence('takken',[run('takken',summary=s,conclusion='failure')]),NOW)
        self.assertEqual(r['condition'],'registration_incomplete')

    def test_preflight_registry_does_not_require_execute_only_counts(self):
        s={'status':'needs_review','mode':'preflight','held_reasons':{'name_email_conflict':1},'business_writes':{'account':0,'enrollment':0,'invite':0},
           'review_registry':{'open':1,'cases':[{'case_id':'a'*64,'input_version':'b'*64,'reason':'name_email_conflict','status':'open'}]}}
        parsed=m.extract_summary(json.dumps(s))
        self.assertNotIn('invalid_counts',parsed)

    def test_business_read_failure_is_unknown_not_healthy(self):
        r=run('bizibl',steps={s:'success' for s in m.BIZIBL_STEPS});r['signals']={'rows_written':2}
        snapshot={'fetched_at':m.iso(NOW),'workflows':[evidence('bizibl',[r])]}
        report,_=m.build_report(snapshot)
        def fail(*args):raise RuntimeError('secret raw failure')
        e.enrich(report,snapshot,check_sheets=fail)
        result=next(r for r in report['results'] if r['key']=='bizibl')
        self.assertEqual(result['state'],'unknown')
        self.assertNotIn('secret raw',json.dumps(report))

    def test_aggregate_failure_does_not_mask_successful_source_sync(self):
        r=run('bizibl',steps={s:'success' for s in m.BIZIBL_STEPS});r['signals']={'rows_written':2}
        snapshot={'fetched_at':m.iso(NOW),'workflows':[evidence('bizibl',[r])]}
        report,_=m.build_report(snapshot)
        with patch.object(m.GitHubReader,'get',return_value={'workflow_runs':[{'id':r['id'],'status':'completed'}]}):
            e.enrich(report,snapshot,check_sheets=lambda _:dict(source_rows=2,shared_rows=1,shared_columns=2),
                     check_downstream=lambda *_:[{'name':'広告実績','status':'failed'}])
        result=next(r for r in report['results'] if r['key']=='bizibl')
        self.assertEqual(result['condition'],'downstream_incomplete')
        self.assertIn('2行',result['done'])


if __name__=='__main__':unittest.main()
