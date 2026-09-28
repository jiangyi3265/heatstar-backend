import assert from 'node:assert/strict'

const values = new Map()
let finishRequest
globalThis.uni = {
  getStorageSync: key => values.get(key) ?? '',
  setStorageSync: (key, value) => values.set(key, value),
  removeStorageSync: key => values.delete(key),
  request: options => { finishRequest = options }
}
const { saveProposalConfirmed, STORAGE_KEYS } = await import('../utils/storage.js')
const { accountingMonth, scoreFor, personalSummary, reviewPendingCount } = await import('../utils/stats.js')
const { inReviewScope } = await import('../utils/organization.js')
const { parseAttendeeText, personalShareCount } = await import('../utils/sharing.js')

const original = { id: 'QA', status: 'pending_team' }
values.set(STORAGE_KEYS.proposals, [original])
const pending = saveProposalConfirmed({ ...original, status: 'pending_section' })
assert.equal(values.get(STORAGE_KEYS.proposals)[0].status, 'pending_team')
finishRequest.success({ statusCode: 200, data: { ok: true, proposal: { ...original, status: 'pending_section', reviewedAt: '2026-09-05 10:00' } } })
const saved = await pending
assert.equal(saved.status, 'pending_section')
assert.equal(values.get(STORAGE_KEYS.proposals)[0].reviewedAt, '2026-09-05 10:00')

const denied = saveProposalConfirmed({ ...saved, status: 'approved' })
finishRequest.success({ statusCode: 403, data: { ok: false, message: '无权限' } })
await assert.rejects(denied, /无权限/)
assert.equal(values.get(STORAGE_KEYS.proposals)[0].status, 'pending_section')

const offline = saveProposalConfirmed({ ...saved, status: 'pending_factory' })
finishRequest.fail({ errMsg: 'network unavailable' })
await assert.rejects(offline, /network unavailable/)
assert.equal(values.get(STORAGE_KEYS.proposals)[0].status, 'pending_section')

assert.equal(accountingMonth('2026-09-25 23:59', 25), '2026-09')
assert.equal(accountingMonth('2026-09-26 00:00', 25), '2026-10')
assert.equal(accountingMonth('2026-12-26', 25), '2027-01')
assert.equal(scoreFor(3, { oneStar: 1, twoStar: 3, threeStar: 5 }), 5)
const person = { workNo: 'QA-01', name: '测试人员', team: 'QA' }
const attendees = parseAttendeeText('QA-01 测试人员 QA-01', [person])
assert.equal(attendees.length, 1)
assert.equal(personalShareCount([{ id: 'S1', attendees }, { id: 'S1', attendees }, { id: 'S2', host: person.name }], person), 1)
console.log('PASS: server acknowledgement, denied/offline save preservation, month/year cutoff, star points, attendance deduplication and host exclusion')

const departmentUser={workNo:'00123',name:'同名员工',team:'热一组',departmentId:'forging',reviewLevel:'team',scope:'热一组'}
const localRecord={id:'D1',firstAuthor:'同名员工',submitterId:'00123',team:'热一组',departmentId:'forging',status:'approved',stars:2,reviewedAt:'2026-09-15'}
const otherRecord={...localRecord,id:'D2',submitterId:'00234',departmentId:'heat-treatment',stars:3}
const summary=personalSummary(departmentUser,{proposals:[localRecord,otherRecord],month:'2026-09',rules:{oneStar:1,twoStar:3,threeStar:5,cutoffDay:25}})
assert.equal(summary.monthPoints,3)
assert.equal(summary.mineCount,1)
assert.equal(inReviewScope(departmentUser,otherRecord),false)
assert.equal(reviewPendingCount(departmentUser,[{...localRecord,status:'pending_team'},{...otherRecord,status:'pending_team'}]),1)
const groupUser={...departmentUser,reviewLevel:'reviewer',departmentScopes:['forging']}
assert.equal(reviewPendingCount(groupUser,[{...localRecord,status:'pending_review'},{...otherRecord,status:'pending_review'}]),1)
assert.equal(inReviewScope({...groupUser,departmentScopes:['forging','heat-treatment']},otherRecord),true)
console.log('PASS: department scoring isolation, same-name team scope and shared reviewer pool')
