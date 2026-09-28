import copy
import unittest

import company_workflow as flow
import proposals


class CompanyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.states = {'heatstar_people': [dict(workNo=f'R{i}', name=f'评审{i}', level='评审员', status='在岗', departmentId='heat-treatment') for i in range(5)],
                       'heatstar_settings': {'pointRules': {'oneStar':1,'twoStar':3,'threeStar':5}}}
        self.items = []
        self.employee = dict(workNo='E', name='员工', departmentId='heat-treatment')
        self.team = dict(workNo='TL', name='班长', reviewLevel='team', team='热一组', departmentId='heat-treatment')
        self.company = dict(workNo='C', name='公司评审', reviewLevel='company', accessLevel='company', departmentId='other')
        self.specialist = dict(workNo='S', name='精益专员', canRecommend=True, accessLevel='specialist', departmentScopes=['heat-treatment'])
        self.base = dict(id='P', type='six', title='试验提案', submitterId='E', firstAuthor='员工', team='热一组', status='pending_team')

    def save(self, incoming, user):
        self.items, result = flow.merge_one(self.items, incoming, user, self.states, '2026-09-18 12:00')
        return result

    def start(self):
        item = self.save(self.base, self.employee)
        return self.save({**item, 'status':'pending_review','stars':1}, self.team)

    def voter(self, index):
        return dict(workNo=f'R{index}', name=f'评审{index}', reviewLevel='reviewer', departmentScopes=['heat-treatment'])

    def vote(self, index, stars, item=None):
        return self.save({**(item or self.items[0]), 'status':'approved', 'stars':stars,'points':999}, self.voter(index))

    def test_exact_eighty_percent_only_after_every_member_votes(self):
        self.start()
        for i in range(4):
            item = self.vote(i, 3)
            self.assertEqual(item['status'], 'pending_review')
            self.assertEqual(item['points'], 0)
        item = self.vote(4, 1)
        self.assertEqual(item['status'], 'pending_company')
        self.assertEqual(item['departmentReview']['totalScore'], 13)
        self.assertEqual(item['departmentReview']['maxScore'], 15)
        self.assertEqual(item['departmentReview']['averageStars'], 2)
        self.assertEqual(item['companyRoute'], 'threshold')
        self.assertEqual(item['stars'], 0)
        item = self.save({**item, 'status':'approved', 'stars':3, 'points':999}, self.company)
        self.assertEqual((item['status'],item['stars'],item['points'],item['finalReviewLevel']), ('approved',3,5,'company'))
        with self.assertRaises(proposals.ProposalError):
            self.save({**item,'status':'approved','stars':1},self.company)

    def test_score_ratio_does_not_replace_three_star_headcount(self):
        self.start()
        for i, stars in enumerate([3,3,3,2,2]): item = self.vote(i,stars)
        self.assertEqual(item['departmentReview']['totalScore'],13)  # 86.7% points, but only 60% three-star members.
        self.assertEqual((item['status'],item['stars'],item['points']),('approved',2,3))
        self.assertEqual(item['finalReviewLevel'],'department')
        self.assertEqual(item['departmentReview']['threeStarCount'],3)

    def test_average_floors_and_team_score_excluded(self):
        self.states['heatstar_people'] = self.states['heatstar_people'][:3]
        item = self.start()
        for i, stars in enumerate([1,2,2]): item = self.vote(i,stars)
        self.assertEqual(item['departmentReview']['totalScore'],5)
        self.assertEqual(item['departmentReview']['maxScore'],9)
        self.assertEqual(item['stars'],1)
        self.assertEqual(item['points'],1)

    def test_seven_members_need_six_three_star_votes(self):
        self.states['heatstar_people'] += [dict(workNo=f'R{i}',name=str(i),level='评审员') for i in (5,6)]
        self.start()
        for i, stars in enumerate([3,3,3,3,3,2,2]): item=self.vote(i,stars)
        self.assertEqual(item['departmentReview']['requiredThreeStar'],6)
        self.assertEqual(item['status'],'approved')

    def test_recommendation_needs_completed_department_and_reason(self):
        item = self.save(self.base,self.employee)
        with self.assertRaises(proposals.ProposalError): self.save({**item,'reviewAction':'recommend','recommendationReason':'推荐'},self.specialist)
        item=self.save({**item,'status':'pending_review','stars':3},self.team)
        item=self.vote(0,2)
        with self.assertRaises(proposals.ProposalError): self.save({**item,'reviewAction':'recommend','recommendationReason':'推荐'},self.specialist)
        for i in range(1,5): item=self.vote(i,2)
        for user, reason in [(self.voter(0),'推荐'),(self.specialist,'')]:
            with self.assertRaises(proposals.ProposalError): self.save({**item,'reviewAction':'recommend','recommendationReason':reason},user)
        item=self.save({**item,'reviewAction':'recommend','recommendationReason':'具有跨部门推广价值'},self.specialist)
        self.assertEqual(item['status'],'pending_company')
        self.assertEqual(item['points'],0)
        self.assertEqual(item['companyRoute'],'specialist')
        self.assertEqual(item['departmentReview']['averageStars'],2)
        self.assertEqual(item['reviewHistory'][-1]['decision'],'recommend')
        with self.assertRaises(proposals.ProposalError): self.save({**item,'reviewAction':'recommend','recommendationReason':'再推荐'},self.specialist)

    def test_duplicates_scope_and_forged_ballots_cannot_change_outcome(self):
        initial=self.start()
        item=self.vote(0,3,initial)
        before=copy.deepcopy(self.items)
        with self.assertRaises(proposals.ProposalError): self.vote(0,1,initial)
        self.assertEqual(self.items,before)
        with self.assertRaises(proposals.ProposalError):
            self.save({**item,'status':'approved','stars':3}, {**self.voter(1),'departmentScopes':['other']})
        with self.assertRaises(proposals.ProposalError): self.save({**item,'status':'approved','stars':3},self.company)
        with self.assertRaises(proposals.ProposalError): self.save({**item,'status':'approved','stars':3},{'isSuper':True,'workNo':'admin'})
        item=self.save({**initial,'status':'approved','stars':2,'title':'篡改标题','departmentReview':{'completed':True,'totalScore':15},'companyRoute':'threshold'},self.voter(1))
        self.assertEqual(item['title'],'试验提案')
        self.assertEqual(len(item['departmentReview']['votes']),2)
        self.assertEqual(item['status'],'pending_review')

    def test_roster_fixed_and_removed_review_level_has_no_vote(self):
        item=self.start()
        self.states['heatstar_people'].append(dict(workNo='NEW',name='新人',level='评审员'))
        with self.assertRaises(proposals.ProposalError): self.save({**item,'status':'approved','stars':3},dict(workNo='NEW',reviewLevel='reviewer'))
        with self.assertRaises(proposals.ProposalError): self.save({**item,'status':'approved','stars':3},dict(workNo='R0',reviewLevel='section'))
        self.assertEqual(len(item['departmentReview']['members']),5)

    def test_rejection_resubmission_starts_clean_round_preserves_history(self):
        item=self.start()
        item=self.vote(0,2)
        item=self.save({**item,'status':'rejected','rejectReason':'缺少证据'},self.voter(1))
        history=len(item['reviewHistory'])
        item=self.save({**item,'status':'pending_team','departmentReview':{'completed':True}},self.employee)
        self.assertNotIn('departmentReview',item)
        item=self.save({**item,'status':'pending_review','stars':3},self.team)
        self.assertEqual(item['departmentReview']['votes'],[])
        self.assertEqual(len(item['reviewHistory']),history+2)


if __name__ == '__main__': unittest.main()

