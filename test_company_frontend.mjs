import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { REVIEW_LEVELS, canDepartmentVote, canRecommendProposal, inReviewScope } from '../utils/organization.js';
import { reviewPendingCount } from '../utils/stats.js';
const voter={workNo:'R1',reviewLevel:'reviewer',departmentScopes:['heat-treatment']};
const company={workNo:'C',reviewLevel:'company',accessLevel:'company'};
const specialist={workNo:'S',accessLevel:'specialist',canRecommend:true,departmentScopes:['heat-treatment']};
const ballot={members:[{workNo:'R1'},{workNo:'R2'}],votes:[],completed:false};
const p={id:'P',status:'pending_review',departmentReview:ballot};
assert.equal(canDepartmentVote(voter,p),true);
const voted={...p,departmentReview:{...ballot,votes:[{workNo:'R1',stars:3}]}};
assert.equal(canDepartmentVote(voter,voted),false);
assert.equal(reviewPendingCount(voter,[voted]),0);
assert.equal(inReviewScope(company,{departmentId:'new-department'}),true);
assert.deepEqual(REVIEW_LEVELS.company,['company']);
assert.equal(reviewPendingCount(company,[{...p,status:'pending_company',departmentId:'new-department'}]),1);
assert.equal(canRecommendProposal(specialist,p),false);
const completed={...p,status:'approved',finalReviewLevel:'department',departmentReview:{...ballot,completed:true}};
assert.equal(canRecommendProposal(specialist,completed),true);
assert.equal(reviewPendingCount(specialist,[completed]),1);
assert.equal(canRecommendProposal(specialist,{...completed,departmentId:'other'}),false);
assert.equal(canRecommendProposal(specialist,{...completed,status:'pending_company'}),false);
for (const path of ['pages/review/detail.vue','pages/review/list.vue','pages/proposal/detail.vue','pages/admin/permissions.vue','pages/admin/person-edit.vue','components/DepartmentReviewSummary.vue']) {
  const source=await readFile(new URL('../'+path,import.meta.url),'utf8');
  const code=source.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/^import .*$/gm,'').replace('export default','return');
  new Function(code); // Parse all touched Vue logic independently of the production bundle.
  if(path==='pages/proposal/detail.vue') assert.match(code,/components:.*DepartmentReviewSummary/);
}
console.log('PASS company scope, specialist gate, duplicate vote visibility, pending counts and Vue logic syntax');
