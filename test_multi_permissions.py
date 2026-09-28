import copy
import tempfile
import unittest
from pathlib import Path

import os

os.environ.setdefault('HEATSTAR_SUPER_ADMIN_PASSWORD', 'test-super-admin-password')
os.environ.setdefault('HEATSTAR_DEFAULT_PASSWORD', 'test-user-initial-password')

import app
import organization as org


class MultiPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DATA_DIR = Path(self.temp.name)
        app.UPLOAD_DIR = app.DATA_DIR / 'uploads'
        app.DB_PATH = app.DATA_DIR / 'heatstar.sqlite3'
        app.initialize_database()
        app.set_state('heatstar_departments', org.departments(app.get_states()[0]) + [{'id':'other','name':'机加工厂','code':'JJC','status':'启用'}])
        app.set_state('heatstar_teams', [{'id':did,'name':'一班','section':'生产','departmentId':did} for did in ('heat-treatment','other')])
        self.multi = {'id':'MULTI','workNo':'MULTI','name':'兼任厂长','role':'厂长','status':'在岗','departmentId':'heat-treatment','team':'一班',
                      'permissionRoles':['审核者','管理员'], 'permissionRole':'审核者',
                      'levels':['评审员','公司','精益专员'],
                      'levelScopes':{'评审员':['heat-treatment'],'公司':['*'],'精益专员':['heat-treatment']}}
        self.people = [self.multi,
            {'id':'E','workNo':'E','name':'员工','role':'员工','level':'无','status':'在岗','team':'一班'},
            {'id':'TL','workNo':'TL','name':'班长','role':'班组长','level':'班组','status':'在岗','team':'一班'}]
        app.set_state('heatstar_people',self.people)
        app.set_state('heatstar_proposals',[])

    def tearDown(self):
        self.temp.cleanup()

    def user(self):
        return app.auth.describe_user('MULTI')

    def start(self, key='TEST'):
        item=app.save_proposal_atomic(app.auth.describe_user('E'),{'id':key,'type':'lean','title':'兼任测试','status':'pending_team','submitterId':'E','firstAuthor':'员工','team':'一班'})
        return app.save_proposal_atomic(app.auth.describe_user('TL'),{**item,'status':'pending_review','stars':3})

    def test_same_person_department_specialist_company_end_to_end(self):
        user=self.user()
        self.assertEqual(user['reviewLevels'],['reviewer','company'])
        self.assertTrue(user['canRecommend'])
        self.assertTrue(user['isAdmin'])
        item=self.start()
        self.assertEqual([m['workNo'] for m in item['departmentReview']['members']],['MULTI'])
        item=app.save_proposal_atomic(user,{**item,'status':'approved','stars':2})
        self.assertEqual(item['finalReviewLevel'],'department')
        with self.assertRaises(app.proposal_rules.ProposalError):
            app.save_proposal_atomic(user,{**item,'status':'approved','stars':3})
        item=app.save_proposal_atomic(user,{**item,'reviewAction':'recommend','recommendationReason':'跨部门推广'})
        self.assertEqual(item['status'],'pending_company')
        item=app.save_proposal_atomic(user,{**item,'status':'approved','stars':3})
        self.assertEqual((item['finalReviewLevel'],item['stars'],item['points']),('company',3,5))
        self.assertEqual([h['level'] for h in item['reviewHistory']],['team','reviewer','specialist','company'])
        self.assertEqual(item['departmentReview']['votes'][0]['stars'],2)

    def test_company_access_never_expands_department_vote_or_specialist_scope(self):
        self.multi['levelScopes']['精益专员']=['other']
        app.set_state('heatstar_people',self.people)
        user=self.user()
        self.assertTrue(org.in_scope(user,{'departmentId':'other'})) # Company visibility.
        self.assertFalse(org.in_scope(user,{'departmentId':'other'},'reviewer'))
        self.assertFalse(org.in_scope(user,{'departmentId':'heat-treatment'},'specialist'))
        item=self.start()
        item=app.save_proposal_atomic(user,{**item,'status':'approved','stars':2})
        with self.assertRaisesRegex(app.proposal_rules.ProposalError,'管辖范围'):
            app.save_proposal_atomic(user,{**item,'reviewAction':'recommend','recommendationReason':'不可越权'})
        forged_other={**item,'id':'OTHER','departmentId':'other','status':'pending_review'}
        with self.assertRaisesRegex(app.proposal_rules.ProposalError,'管辖范围'):
            app.company_workflow.merge_one([forged_other],{**forged_other,'status':'approved','stars':3},user,app.get_states()[0],'2026-09-18')

    def test_removing_one_duty_preserves_others_without_new_ballot_members(self):
        item=self.start()
        self.multi['levels'].remove('评审员')
        del self.multi['levelScopes']['评审员']
        app.set_state('heatstar_people',self.people)
        user=self.user()
        self.assertEqual(user['reviewLevels'],['company'])
        self.assertTrue(user['canRecommend'])
        self.assertEqual(item['departmentReview']['members'][0]['workNo'],'MULTI')
        with self.assertRaises(app.proposal_rules.ProposalError):
            app.save_proposal_atomic(user,{**item,'status':'approved','stars':3})
        self.assertEqual(app.company_workflow.members_for(item,app.get_states()[0]),[])

    def test_team_scope_not_widened_by_other_duties(self):
        self.multi['levels']=['班组','评审员']
        self.multi['levelScopes']={'班组':['一班'],'评审员':['other']}
        app.set_state('heatstar_people',self.people)
        user=self.user()
        self.assertEqual(user['reviewLevels'],['team','reviewer'])
        self.assertFalse(org.in_scope(user,{'departmentId':'other','team':'一班'},'team'))
        self.assertTrue(org.in_scope(user,{'departmentId':'other'},'reviewer'))

    def test_invalid_grants_rejected_and_legacy_config_unchanged(self):
        states=app.get_states()[0]
        for updates in [
            {'levels':'评审员'}, {'levels':['评审员','评审员']},
            {'levelScopes':{'评审员':['missing'],'公司':['*'],'精益专员':['heat-treatment']}},
            {'levelScopes':{'评审员':[],'公司':['*'],'精益专员':['heat-treatment']}},
            {'permissionRoles':['root']}, {'levels':[],'levelScopes':{'评审员':['heat-treatment']}}]:
            with self.assertRaises(ValueError): org.normalize_people([{**self.multi,**updates}],states)
        legacy={'workNo':'OLD','name':'原评审','role':'员工','level':'评审员','scope':'热处理厂','status':'在岗'}
        app.set_state('heatstar_people',self.people+[legacy])
        old=app.auth.describe_user('OLD')
        self.assertEqual(old['reviewLevels'],['reviewer'])
        self.assertFalse(old['canRecommend'])
        self.assertNotIn('levels',next(p for p in app.get_states()[0]['heatstar_people'] if p['workNo']=='OLD'))

    def test_management_only_does_not_implicitly_gain_company_voting(self):
        self.multi['permissionRoles']=['管理员']
        self.multi['levels']=['公司']
        self.multi['levelScopes']={'公司':['*']}
        app.set_state('heatstar_people',self.people)
        user=self.user()
        self.assertEqual(user['reviewLevels'],[])
        self.assertTrue(user['isAdmin'])
        self.assertTrue(org.in_scope(user,{'departmentId':'other'}))
        self.assertFalse(app.proposal_rules.can_review_at(user,'company'))

