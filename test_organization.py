import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import os

os.environ.setdefault('HEATSTAR_SUPER_ADMIN_PASSWORD', 'test-super-admin-password')
os.environ.setdefault('HEATSTAR_DEFAULT_PASSWORD', 'test-user-initial-password')

import app
import organization as org


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DATA_DIR = Path(self.temp.name)
        app.UPLOAD_DIR = app.DATA_DIR / 'uploads'
        app.DB_PATH = app.DATA_DIR / 'heatstar.sqlite3'
        app.initialize_database()
        self.states, _, _ = app.get_states()

    def tearDown(self):
        self.temp.cleanup()

    def test_admin_company_scope_includes_new_departments_without_promoting_others(self):
        employee_before = app.auth.describe_user('56348')
        admin = app.auth.describe_user('admin')
        self.assertTrue(admin['isAdmin'])
        self.assertTrue(admin['isSuper'])
        self.assertEqual(admin['level'], '公司')
        self.assertEqual(admin['scope'], '全公司')
        self.assertEqual(admin['accessLevel'], 'company')
        self.assertTrue(all(app.auth.can_write_key(admin, key) for key in app.auth.ADMIN_ONLY_KEYS))
        # A newly created peer department must not need a separate admin grant.
        app.set_state('heatstar_departments', org.departments(self.states) + [
            {'id':'forging','name':'锻造厂','code':'DZC','status':'启用'}])
        states, _, _ = app.get_states()
        refreshed = app.auth.describe_user('admin')
        self.assertIn('forging', refreshed['departmentScopes'])
        other = {'id':'OTHER-1','departmentId':'forging','team':'新班组','submitterId':'OTHER','type':'lean'}
        self.assertTrue(org.in_scope(refreshed, other))
        self.assertTrue(org.in_scope(admin, other))
        items, label = app.export_access.visible_proposals(refreshed, {
            **states, 'heatstar_proposals':[other, {'id':'LOCAL-1','departmentId':'heat-treatment'}]})
        self.assertEqual({item['id'] for item in items}, {'OTHER-1','LOCAL-1'})
        self.assertEqual(label, '全公司提案')
        self.assertEqual(app.auth.describe_user('56348'), employee_before)
        self.assertFalse(org.in_scope(employee_before, other))

    def test_department_validation_and_same_name_team_scope(self):
        departments = org.departments(self.states) + [{'id':'forging','name':'锻造厂','code':'DZC','status':'启用'}]
        app.set_state('heatstar_departments', departments)
        team = {'id':'DZ-T1','name':'热一组','section':'生产工段','departmentId':'forging'}
        app.set_state('heatstar_teams', self.states['heatstar_teams'] + [team])
        states, _, _ = app.get_states()
        normalized = org.normalize_people([{'workNo':'DZ01','name':'新员工','teamId':team['id'],'team':team['name'],'departmentId':'forging'}], states)[0]
        self.assertEqual(normalized['department'], '锻造厂')
        self.assertEqual(normalized['section'], '生产工段')
        local_team = {'workNo':'T1','reviewLevel':'team','team':'热一组','departmentId':'heat-treatment'}
        proposal = {'departmentId':'forging','team':team['name'],'section':team['section']}
        self.assertFalse(org.in_scope(local_team, proposal))
        self.assertTrue(org.in_scope({**local_team,'departmentId':'forging'}, proposal))
        with self.assertRaisesRegex(ValueError, '不能删除'):
            app.set_state('heatstar_departments', org.departments(self.states))
        incoming = {'teamId':team['id'],'team':team['name'],'departmentId':'forging'}
        with self.assertRaisesRegex(ValueError, '跨部门'):
            org.submission(incoming, None, local_team, states)
        out = org.submission(incoming, None, normalized, states)
        self.assertEqual(out['teamId'], 'DZ-T1')
        app.set_state('heatstar_departments', [{**d,'status':'停用'} if d['id']=='forging' else d for d in departments])
        states, _, _ = app.get_states()
        with self.assertRaisesRegex(ValueError, '停用'):
            org.submission(incoming, None, normalized, states)

    def test_group_membership_is_explicit_and_department_scoped(self):
        people = self.states['heatstar_people'] + [{'id':'R1','workNo':'R1','name':'评审员甲','level':'评审员','role':'员工','scope':'热处理厂','team':'行政组','status':'在岗'}]
        app.set_state('heatstar_people', people)
        reviewer = app.auth.login('R1',app.auth.DEFAULT_PASSWORD)['user']
        self.assertEqual(reviewer['reviewLevel'], 'reviewer')
        self.assertEqual(reviewer['departmentScopes'], ['heat-treatment'])
        self.assertFalse(app.proposal_rules.can_review_at(reviewer, 'team'))
        self.assertTrue(app.proposal_rules.can_review_at(reviewer, 'reviewer'))
        self.assertFalse(org.in_scope(reviewer, {'departmentId':'forging'}))
        self.assertEqual(app.auth.describe_user('54782')['reviewLevel'], '')
        self.assertEqual(app.auth.describe_user('49598')['reviewLevel'], '')

    def test_migration_is_idempotent_preserves_completed_records(self):
        history = [{'level':'section','reviewer':'原评审人','stars':2,'time':'2026-09-01'}]
        items = [{'id':'OLD1','status':'pending_factory','reviewHistory':history,'stars':2},
                 {'id':'DONE','status':'approved','stars':3,'points':5,'reviewHistory':history}]
        app.set_state('heatstar_proposals', items)
        app.migrate_workflow()
        states, meta, _ = app.get_states()
        migrated = states['heatstar_proposals']
        self.assertEqual(migrated[0]['status'], 'pending_review')
        self.assertEqual(migrated[0]['reviewHistory'], history)
        self.assertEqual(migrated[1], items[1])
        app.migrate_workflow()
        self.assertEqual(app.get_states()[1], meta)

    def test_company_reviewer_manager_specialist_and_removed_section(self):
        profiles = [
            {'workNo':'CORP-R','level':'公司','permissionRole':'审核者'},
            {'workNo':'CORP-A','level':'公司','permissionRole':'管理员','isAdmin':True},
            {'workNo':'LEAN-S','level':'精益专员','permissionRole':'审核者'},
            {'workNo':'OLD-SECTION','role':'工段长'},
        ]
        app.set_state('heatstar_people', self.states['heatstar_people'] + [
            {'id':p['workNo'],'name':p['workNo'],'role':'员工','status':'在岗','team':'行政组',**p} for p in profiles])
        reviewer=app.auth.describe_user('CORP-R')
        manager=app.auth.describe_user('CORP-A')
        specialist=app.auth.describe_user('LEAN-S')
        self.assertEqual(reviewer['reviewLevel'],'company')
        self.assertFalse(reviewer['isAdmin'])
        self.assertTrue(org.in_scope(reviewer,{'departmentId':'forging'}))
        self.assertFalse(app.proposal_rules.can_review_at(reviewer,'reviewer'))
        self.assertTrue(manager['isAdmin'])
        self.assertEqual(manager['reviewLevel'],'')
        self.assertTrue(org.in_scope(manager,{'departmentId':'forging'}))
        self.assertTrue(specialist['canRecommend'])
        self.assertFalse(specialist['canReview'])
        self.assertFalse(specialist['isAdmin'])
        self.assertTrue(org.in_scope(specialist,{'departmentId':'heat-treatment'}))
        self.assertFalse(org.in_scope(specialist,{'departmentId':'forging'}))
        self.assertEqual(app.auth.describe_user('OLD-SECTION')['reviewLevel'],'')

    def test_department_numbering_keeps_original_namespace(self):
        now = __import__('datetime').datetime(2026,9,15)
        for kind, prefix in [('lean','JY'),('tool','GJ'),('opl','OPL')]:
            self.assertEqual(app.proposal_rules.number_prefix(kind,self.states,now), f'{prefix}-RT-2026-')
            self.assertEqual(app.proposal_rules.number_prefix(kind,self.states,now,{'id':'forging','code':'DZC'}), f'{prefix}-DZC-2026-')

    def test_ledger_has_all_types_numbers_dates_history_and_safe_text(self):
        item = {'id':'00001','title':'=HYPERLINK("bad")','type':'six','status':'pending_review','submitterId':'00123',
                'firstAuthor':'测试作者','submittedAt':'2026-09-15 10:30','stars':2,'points':0,'savedAmount':'123.45',
                'reviewHistory':[{'level':'team','levelLabel':'班组长','reviewer':'班长','decision':'approve','stars':2,'time':'2026-09-15 11:00'}]}
        workbook = app.export_ledger.build([item, {**item,'id':'GJ1','type':'tool'},{**item,'id':'OPL1','type':'opl'}],self.states)
        with zipfile.ZipFile(io.BytesIO(workbook)) as z:
            self.assertIsNone(z.testzip())
            for name in z.namelist():
                if name.endswith('.xml') or name.endswith('.rels'):
                    ET.fromstring(z.read(name))
            ns = {'m': app.export_ledger.NS}
            sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            cells = {c.attrib['r']:c for c in sheet.findall('.//m:c',ns)}
            self.assertEqual(cells['B3'].attrib['t'],'inlineStr')
            self.assertEqual(cells['B3'].find('.//m:t',ns).text,'00001')
            self.assertEqual(cells['H3'].attrib['t'],'inlineStr')
            self.assertFalse(sheet.findall('.//m:f',ns))
            self.assertEqual(float(cells['P3'].find('m:v',ns).text),123.45)
            self.assertIsNone(cells['S3'].find('m:v',ns))
            self.assertIsNone(cells['T3'].find('m:v',ns))
            self.assertEqual(sheet.find('m:autoFilter',ns).attrib['ref'],'A2:U3')
            self.assertIn('评审小组审核中', z.read('xl/worksheets/sheet1.xml').decode())
            self.assertIn('班长', z.read('xl/worksheets/sheet4.xml').decode())


if __name__ == '__main__':
    unittest.main()

