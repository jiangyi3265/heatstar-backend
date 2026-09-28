import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import * as permissions from '../utils/permissions.js'
import * as organization from '../utils/organization.js'
import { departmentReviewers, reviewDepartments } from '../utils/review-center.js'
import { reviewPendingCount } from '../utils/stats.js'
const departments=[{id:'heat-treatment',name:'热处理厂'},{id:'other',name:'机加工厂'}]
const legacy={id:'R',workNo:'R',name:'测试厂长',level:'评审员',scope:'热处理厂',permissionRole:'审核者'}
const source=await readFile(new URL('../pages/admin/permissions.vue',import.meta.url),'utf8')
const code=source.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/^import .*$/gm,'').replace('export default','return')
const bindings={AppHeader:null,AppIcon:null,...permissions,...organization}
const options=new Function(...Object.keys(bindings),code)(...Object.values(bindings))
const page=options.data()
for (const [key,method] of Object.entries(options.methods)) page[key]=method.bind(page)
for (const [key,getter] of Object.entries(options.computed)) Object.defineProperty(page,key,{get:()=>getter.call(page)})
page.people=[legacy]; page.departments=departments; page.draft=permissions.permissionDraft(legacy,departments)
page.changeLevel('精益专员'); page.changeLevel('公司'); page.toggleRole('管理员')
assert.deepEqual(page.draft.levels,['评审员','精益专员','公司'])
assert.deepEqual(page.draft.permissionRoles,['审核者','管理员'])
page.toggleScope('精益专员','heat-treatment'); page.toggleScope('精益专员','other')
const fields=permissions.permissionFields(page.draft,departments)
assert.deepEqual(fields.levelScopes['评审员'],['heat-treatment'])
assert.deepEqual(fields.levelScopes['精益专员'],['other'])
assert.deepEqual(permissions.permissionDraft(fields,departments),page.draft)
assert.equal(departmentReviewers([{...legacy,...fields}],departments,'heat-treatment').length,1)
assert.equal(departmentReviewers([{...legacy,...fields}],departments,'other').length,0)
page.changeLevel('公司')
assert.deepEqual(page.draft.levels,['评审员','精益专员'])
assert.equal(page.validScopes,true)
let saveRequested=false
page.commitSave=()=>{saveRequested=true}
page.savePermission()
assert.equal(saveRequested,true)

const user={workNo:'R',reviewLevel:'reviewer',reviewLevels:['reviewer','company'],canRecommend:true,scopeByLevel:{reviewer:{departmentId:'heat-treatment',departmentScopes:['heat-treatment']},specialist:{departmentId:'heat-treatment',departmentScopes:['other']},company:{departmentId:'heat-treatment'}}}
const pending={id:'P',departmentId:'heat-treatment',status:'pending_review',departmentReview:{members:[{workNo:'R'}],votes:[]}}
const completed={id:'D',departmentId:'other',status:'approved',finalReviewLevel:'department',departmentReview:{completed:true}}
assert.equal(organization.canDepartmentVote(user,pending),true)
assert.equal(organization.canDepartmentVote(user,{...pending,departmentId:'other'}),false)
assert.equal(organization.canRecommendProposal(user,completed),true)
assert.equal(organization.canRecommendProposal(user,{...completed,departmentId:'heat-treatment'}),false)
assert.deepEqual(reviewDepartments(user,departments,'reviewer'),[departments[0]])
assert.deepEqual(reviewDepartments(user,departments,'specialist'),[departments[1]])
assert.deepEqual(reviewDepartments(user,departments,'company'),departments)
assert.equal(reviewPendingCount(user,[pending,{...pending,id:'OUT',departmentId:'other'},completed,{id:'C',status:'pending_company',departmentId:'other'}]),3)
console.log('PASS multi-role editor round-trip, independent scopes, combined queues and no cross-duty privilege expansion')
