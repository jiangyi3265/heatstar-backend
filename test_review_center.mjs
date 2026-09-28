import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import * as organization from '../utils/organization.js'
import * as center from '../utils/review-center.js'

const departments = [{id:'heat-treatment',name:'热处理厂'}, {id:'machining',name:'机加工厂'}]
const reviewer = {workNo:'R1',reviewLevel:'reviewer',departmentScopes:['heat-treatment']}
const superUser = {workNo:'admin',isSuper:true,reviewLevel:'reviewer',accessLevel:'company'}
const people = [
  {workNo:'R1',name:'王评审',level:'评审员',departmentId:'heat-treatment'},
  {workNo:'R2',name:'李评审',level:'评审员',scope:'机加工厂'},
  {workNo:'R3',name:'跨部门评审',level:'评审员',departmentScopes:['heat-treatment','machining']},
  {workNo:'OFF',level:'评审员',status:'离岗'},
  {workNo:'EMP',level:'评审员',permissionRole:'员工'},
  {workNo:'C',level:'公司'}, {workNo:'LEADER',level:'班组'}
]
assert.deepEqual(center.departmentReviewers(people,departments,'heat-treatment').map(p=>p.workNo),['R1','R3'])
assert.deepEqual(center.departmentReviewers(people,departments,'machining').map(p=>p.workNo),['R2','R3'])
assert.deepEqual(center.reviewDepartments(reviewer,departments),[departments[0]])
assert.equal(center.reviewDepartments(superUser,departments).length,2)

const proposal = {id:'P1',departmentId:'heat-treatment',status:'pending_review',departmentReview:{members:[{workNo:'R1'},{workNo:'R3'}],votes:[{workNo:'R1',stars:3}],completed:false},reviewHistory:[{level:'reviewer',reviewerWorkNo:'R1'}]}
assert.equal(center.departmentProgress(proposal),'部门评审进行中 · 已评 1/2 人')
assert.equal(center.processedAtLevel(proposal,'reviewer',reviewer),true)
assert.equal(center.processedAtLevel(proposal,'company',superUser),false)
assert.equal(center.processedAtLevel({status:'rejected',reviewHistory:[{level:'team',reviewerWorkNo:'TL'}]},'reviewer',superUser),false)
assert.equal(center.departmentProgress({...proposal,departmentReview:{...proposal.departmentReview,members:[{workNo:'R1'}]}}),'部门评审进行中 · 已评 1/1 人')

// Exercise the real list computed properties, including department filters and stage isolation.
const source = await readFile(new URL('../pages/review/list.vue',import.meta.url),'utf8')
const code = source.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/^import .*$/gm,'').replace('export default','return')
const bindings = {AppHeader:null,AppIcon:null,StatusTag:null,currentUser:()=>superUser,...organization,...center}
const options = new Function(...Object.keys(bindings),code)(...Object.values(bindings))
const page = options.data()
for (const [name, method] of Object.entries(options.methods)) page[name] = method.bind(page)
for (const [name, getter] of Object.entries(options.computed)) Object.defineProperty(page,name,{get:()=>getter.call(page)})
page.departments=departments; page.people=people; page.activeLevel='reviewer'; page.selectedDepartment='heat-treatment'
page.proposals=[proposal, {...proposal,id:'P2',departmentId:'machining'}, {...proposal,id:'P3',status:'pending_company',departmentReview:{...proposal.departmentReview,completed:true}}, {id:'P4',departmentId:'heat-treatment',status:'rejected',reviewHistory:[{level:'team',reviewerWorkNo:'TL'}]}]
assert.deepEqual(page.levels.map(l=>l.label),['班组初审','部门评审','公司评审','精益推荐'])
assert.deepEqual(page.pendingItems.map(p=>p.id),['P1'])
assert.deepEqual(page.processedItems.map(p=>p.id),['P1','P3'])
assert.deepEqual(page.departmentGroups.map(g=>g.id),['heat-treatment'])
page.changeLevel('company')
assert.deepEqual(page.pendingItems.map(p=>p.id),['P3'])
assert.equal(page.processedCount,0)
page.changeDepartment({detail:{value:2}})
assert.equal(page.pendingCount,0)
page.changeLevel('reviewer'); page.user=reviewer; page.selectedDepartment='heat-treatment'
assert.equal(page.pendingCount,0) // Own vote already submitted; still visible in processed.
assert.deepEqual(page.levels.map(l=>l.value),['reviewer'])
assert.deepEqual(page.departmentGroups.map(g=>g.id),['heat-treatment'])
console.log('PASS department member scopes, inactive exclusions, fixed ballot progress, stage histories and actual list filtering')
