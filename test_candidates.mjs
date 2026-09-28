import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { personalShareCount } from '../utils/sharing.js'
import { proposalParticipants } from '../utils/participation.js'
import { departmentId, departmentName } from '../utils/organization.js'
import { isMine } from '../utils/stats.js'

// Exercise the page's business methods without a browser or production database.
const source = await readFile(new URL('../pages/points/candidates.vue', import.meta.url), 'utf8')
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/^import .*$/gm, '').replace('export default', 'return')
const day = new Date().toISOString().slice(0, 10)
const store = {
  people: [{ workNo: 'Q1', name: '测试员工', team: '维修组' }, { workNo: 'Q2', name: '真实受限员工', team: '二组', compliance: false, complianceReason: '已记录事件' }],
  teams: [{ id: 'T1', name: '维修组' }, { id: 'T2', name: '二组' }],
  settings: { oneStar: 1, twoStar: 3, threeStar: 5 },
  shares: [1, 2].map(id => ({ id: 'S' + id, date: day, team: '维修组', attendees: [{ name: '测试员工', workNo: 'Q1' }] })),
  candidates: { people: [], teams: [], logs: [] }
}
const proposals = [1, 2, 3].map(id => ({ id: String(id), status: 'approved', reviewedAt: day, stars: 3, firstAuthor: '测试员工', team: '维修组' }))
const deps = {
  AppHeader: {}, AppIcon: {}, StatusTag: {}, pointRules: store.settings,
  STORAGE_KEYS: { people: 'people', teams: 'teams', settings: 'settings', shares: 'shares', candidates: 'candidates' },
  readStorage: key => store[key], getProposals: () => proposals, personalShareCount, proposalParticipants, departmentId, departmentName, isMine
}
const page = new Function(...Object.keys(deps), script)(...Object.values(deps))
const vm = { ...page.data(), ...page.methods }
vm.loadCandidates()
assert.equal(vm.people.length, 2, 'must not append demo people')
assert.equal(vm.teamCandidates[0].compliance, true, 'team name must not imply a compliance incident')
assert.equal(vm.people[1].compliance, false, 'actual compliance restrictions must remain')
assert.equal(vm.people[0].points, 15)
assert.equal(vm.people[0].shares, 2)
assert.equal(vm.automaticEligible(vm.people[0]), true)
store.candidates.teams = [{ id: 'CT-T1', name: '维修组', compliance: false, complianceReason: '真实保存的限制' }]
vm.loadCandidates()
assert.equal(vm.teamCandidates[0].compliance, false)
assert.equal(vm.teamCandidates[0].complianceReason, '真实保存的限制')
console.log('PASS actual roster only, no name-based compliance restrictions, saved compliance preserved, points and attendance eligibility')
store.people.push({workNo:'DZ01',name:'测试员工',team:'维修组',departmentId:'forging'})
store.teams.push({id:'DZ-T1',name:'维修组',departmentId:'forging'})
proposals.push({id:'DZ1',status:'approved',reviewedAt:day,stars:2,firstAuthor:'测试员工',submitterId:'DZ01',team:'维修组',departmentId:'forging'})
vm.loadCandidates()
assert.equal(vm.people[0].points,15)
assert.equal(vm.people[2].points,3)
assert.equal(vm.teamCandidates[0].total,15)
assert.equal(vm.teamCandidates[2].total,3)
assert.equal(vm.teamCandidates[2].hasStar,false)
console.log('PASS candidate scoring remains isolated across same-name departments and teams')
