import { readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const serverDir = resolve(fileURLToPath(new URL('.', import.meta.url)))
const mockPath = resolve(serverDir, '../utils/mock.js')
const source = await readFile(mockPath, 'utf8')
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const mock = await import(moduleUrl)

const state = {
  heatstar_proposals: mock.proposals,
  heatstar_people: mock.people,
  heatstar_teams: mock.teams,
  heatstar_permissions: { records: [], logs: mock.permissionLogs },
  heatstar_notices: mock.announcements,
  heatstar_settings: {
    workshopCode: 'RTC',
    sequenceDigits: 4,
    resetCycle: 'monthly',
    oneStar: mock.pointRules.oneStar,
    twoStar: mock.pointRules.twoStar,
    threeStar: mock.pointRules.threeStar,
    cutoffDay: mock.pointRules.cutoffDay
  },
  heatstar_candidates: {
    version: 1,
    people: [],
    teams: [],
    logs: []
  },
  heatstar_shares: mock.sharingRecords,
  heatstar_categories: mock.sourceCategories,
  heatstar_honor_results: [mock.honorResults, ...mock.quarterHistory.map(item => ({ ...item, archived: true }))]
}

await writeFile(resolve(serverDir, 'seed_state.json'), `${JSON.stringify(state, null, 2)}\n`, 'utf8')
console.log(`Generated ${Object.keys(state).length} state collections.`)
