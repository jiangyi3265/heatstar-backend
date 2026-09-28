import io
import unittest
import zipfile
from xml.etree import ElementTree as ET

import export_ledger as ledger

NS = {'m': ledger.NS}
STATES = {'heatstar_departments': [
    {'id': 'heat-treatment', 'name': '热处理厂', 'code': 'RT', 'status': '启用'},
    {'id': 'forging', 'name': '锻造厂', 'code': 'DZ', 'status': '启用'}]}


def read_book(items):
    with zipfile.ZipFile(io.BytesIO(ledger.build(items, STATES))) as z:
        assert z.testzip() is None
        for path in z.namelist():
            if path.endswith(('.xml', '.rels')):
                ET.fromstring(z.read(path))
        names = ET.fromstring(z.read('xl/workbook.xml')).findall('m:sheets/m:sheet', NS)
        return {s.attrib['name']: ET.fromstring(z.read(f'xl/worksheets/sheet{i}.xml')) for i, s in enumerate(names, 1)}


def value(sheet, ref):
    c = sheet.find(f'.//m:c[@r="{ref}"]', NS)
    if c is None:
        return None
    if c.attrib.get('t') == 'inlineStr':
        return c.find('m:is/m:t', NS).text
    n = c.find('m:v', NS)
    return float(n.text) if n is not None else None


class LedgerTests(unittest.TestCase):
    def item(self, kind='six', **fields):
        return dict({'id': '00001', 'sourceNo': '00123', 'type': kind, 'title': '=HYPERLINK("bad")',
                     'departmentId': 'heat-treatment', 'team': '热四组', 'firstAuthor': '张三',
                     'submittedAt': '2026-09-15 10:30', 'discoverDate': '2026-08-31',
                     'improveDate': '2026-09-02', 'discoverer': '李四', 'categories': ['故障源', '危险源'],
                     'problem': '柜门开关损坏', 'measures': '更换开关', 'solvingUnit': '维修组', 'solver': '王五',
                     'savedAmount': '123.45', 'status': 'approved', 'stars': 3, 'points': 7}, **fields)

    def test_all_four_templates_groups_and_per_sheet_numbering(self):
        book = read_book([self.item(k) for k in ('six', 'tool', 'opl', 'lean')])
        self.assertEqual(list(book), ['精益提案台账', '六源管理台账', '工具开发台账', 'OPL单点课台账', '评审记录'])
        for name, sheet in book.items():
            if name == '评审记录':
                continue
            start = 3 if name in ('精益提案台账', '六源管理台账') else 4
            self.assertEqual(value(sheet, f'A{start}'), 1)
            self.assertEqual(value(sheet, f'B{start}'), '00123')
            self.assertFalse(sheet.findall('.//m:f', NS))
            self.assertTrue(sheet.find('m:mergeCells', NS) is not None)
        opl = book['OPL单点课台账']
        self.assertEqual(value(opl, 'E2'), '人员信息')
        self.assertEqual(value(opl, 'H2'), '提报日期')
        self.assertEqual(value(opl, 'M2'), '培训日期')
        self.assertEqual(value(opl, 'P2'), '评审结果')
        self.assertEqual(value(book['工具开发台账'], 'A2'), '基本信息')

    def test_six_old_columns_and_final_scores(self):
        s = read_book([self.item()])['六源管理台账']
        expected = {'C3':'热处理厂','D3':'热四组','E3':'李四','F3':2026,'G3':8,'H3':'柜门开关损坏',
                    'I3':'故障源、危险源','J3':'是','K3':None,'L3':'维修组','M3':'王五','N3':'更换开关',
                    'P3':123.45,'Q3':None,'R3':None,'S3':3,'T3':7,'U3':'已采纳'}
        for ref, expected_value in expected.items():
            self.assertEqual(value(s, ref), expected_value, ref)
        self.assertEqual(value(s, 'O3'), ledger.date_value('2026-09-02'))
        self.assertEqual(value(s, 'S2'), '终审星级')
        self.assertEqual(s.find('m:autoFilter', NS).attrib['ref'], 'A2:U3')

    def test_lean_adapts_six_without_inventing_missing_fields(self):
        item = self.item('lean', problem='', beforeText='改善前描述', measures='', afterText='改善后描述', projectType='质量改善',
                         discoverDate='', improveDate='', solvingUnit='', solver='', stars=2, points='3.5')
        s = read_book([item])['精益提案台账']
        for ref, expected in {'B2':'提案编号','E2':'提报人姓名','F2':'提报年份','G2':'提报月份','I2':'提案类别',
                              'E3':'张三','G3':9,'H3':'改善前描述','I3':'质量改善','N3':'改善后描述','O3':None,'S3':2,'T3':3.5}.items():
            self.assertEqual(value(s, ref), expected, ref)

    def test_pending_rejected_and_historical_points(self):
        items = [self.item(status='pending_review', stars=2, points=3, rejectReason='旧的原因'),
                 self.item(status='rejected', stars=2, points=3, rejectReason='未解决问题'),
                 self.item(status='approved', stars=1, points=0), self.item(status='approved', stars=3, points=None)]
        s = read_book(items)['六源管理台账']
        for row in (3, 4):
            self.assertIsNone(value(s, f'S{row}'))
            self.assertIsNone(value(s, f'T{row}'))
        self.assertEqual(value(s, 'J3'), '待评审')
        self.assertIsNone(value(s, 'K3'))
        self.assertEqual(value(s, 'J4'), '否')
        self.assertEqual(value(s, 'K4'), '未解决问题')
        self.assertEqual(value(s, 'T5'), 0)
        self.assertIsNone(value(s, 'T6'))

    def test_tool_opl_dates_training_and_cross_department_names(self):
        opl = self.item('opl', oplCategory='其他', otherCategory='安全操作', trainingDate='2026-09-16',
                        trainingEvaluation='能独立进行', trainees='热四组全员')
        items = [self.item('tool'), self.item('tool', departmentId='forging'), opl,
                 {**opl, 'trainingEvaluation':'', 'trainingCount':None}, {**opl, 'trainingCount':'12'}]
        book = read_book(items)
        tool = book['工具开发台账']
        self.assertIn('多部门2026年', value(tool, 'A1'))
        self.assertEqual(value(tool, 'H5'), '锻造厂')
        self.assertEqual(value(tool, 'G4'), ledger.date_value('2026-09-15 10:30'))
        self.assertEqual(value(tool, 'I4'), 3)
        self.assertEqual(value(tool, 'J4'), 7)
        s = book['OPL单点课台账']
        for ref, expected in {'D4':'其他：安全操作','H4':2026,'I4':9,'J4':15,'K4':'是','L4':None,'M4':2026,'N4':9,'O4':16,'P4':3,'Q4':7,'K5':None,'L6':12}.items():
            self.assertEqual(value(s, ref), expected, ref)

    def test_history_single_type_and_hostile_text(self):
        item = self.item('tool', reviewHistory=[None, {'level':'team','reviewer':'班长','reviewerWorkNo':'0009','decision':'approve','stars':2,'time':'2026-09-15 11:00','comment':'=2+2\x00'}])
        book = read_book([item])
        self.assertEqual(list(book), ['工具开发台账', '评审记录'])
        self.assertEqual(value(book['工具开发台账'], 'C4'), '=HYPERLINK("bad")')
        h = book['评审记录']
        self.assertEqual(value(h, 'F4'), '0009')
        self.assertEqual(value(h, 'H4'), 2)
        self.assertEqual(value(h, 'J4'), '=2+2')
        self.assertEqual(value(h, 'I4'), ledger.date_value('2026-09-15 11:00'))
        self.assertFalse(h.findall('.//m:f', NS))


if __name__ == '__main__':
    unittest.main()
