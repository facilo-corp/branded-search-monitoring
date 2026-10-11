"""Real Git history verifies that routine outputs cannot disable monitoring."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import runtime


class ApprovedCheckout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.before = Path.cwd()
        os.chdir(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'core.autocrlf', 'false')
        for name in ('automation-monitor/runtime.py', 'automation-monitor/authorization.json',
                     '.github/workflows/automation-monitor.yml', '.gitattributes', 'site/index.html'):
            p=Path(name); p.parent.mkdir(parents=True, exist_ok=True); p.write_text('approved\n')
        self.base=self.commit()

    def tearDown(self):
        os.chdir(self.before)
        # Windows Git objects can be read-only; TemporaryDirectory handles this.
        self.tmp.cleanup()

    def git(self,*args):
        return subprocess.check_output(['git',*args],text=True,stderr=subprocess.DEVNULL).strip()

    def commit(self):
        self.git('add','.')
        self.git('commit','-qm','fixture')
        return self.git('rev-parse','HEAD')

    def check(self,head,base=None):
        with patch.dict(os.environ, {'APPROVED_MONITOR_SHA':base or self.base,'GITHUB_SHA':head}):
            return runtime.approved_checkout()

    def test_initial_and_published_dashboard_are_allowed(self):
        self.assertTrue(self.check(self.base))
        Path('site/index.html').write_text('next weekly publication\n')
        self.assertTrue(self.check(self.commit()))

    def test_monitor_policy_workflow_and_checkout_rules_require_review(self):
        for name in ('automation-monitor/runtime.py','automation-monitor/authorization.json',
                     '.github/workflows/automation-monitor.yml','.gitattributes'):
            with self.subTest(name=name):
                self.git('reset','--hard',self.base)
                Path(name).write_text('unreviewed\n')
                self.assertFalse(self.check(self.commit()))

    def test_missing_pin_and_wrong_checkout_are_rejected(self):
        self.assertFalse(self.check(self.base,'a'*40))
        self.assertFalse(self.check('b'*40))
        self.assertFalse(self.check(self.base,'not-a-sha'))


if __name__ == '__main__':
    unittest.main()
