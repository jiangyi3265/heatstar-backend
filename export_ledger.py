"""Type-specific factory ledgers, using the service's standard-library OOXML stack."""
import datetime as dt
import io
import math
import re
import zipfile
from xml.sax.saxutils import escape

import organization

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
TYPES = {'lean': '精益提案', 'six': '六源管理', 'tool': '工具开发', 'opl': 'OPL单点课'}
STATUS = {'pending_team': '班组长审核中', 'pending_review': '评审小组审核中',
          'pending_company': '公司审核中',
          'pending_section': '原工段长待审', 'pending_factory': '原厂长待审',
          'approved': '已采纳', 'rejected': '已驳回'}
SIX_HEADERS = ['序号', '六源编号', '分厂/车间/部门', '班组/岗位', '发现人', '发现年份',
               '发现月份', '六源问题简述', '六源类别', '是否采纳', '不采纳原因', '解决单位',
               '解决人', '改善措施', '改善日期', '改善节约成本金额（元）\n（成本节约按每年计算，没有成本节约的可不填）',
               '节约备注', '奖励金额（元）']
TOOL_HEADERS = ['序号', '工具编号', '工具名称', '工具类别', '班组/工段', '提报人姓名', '提报日期', '所属部门']
OPL_HEADERS = ['序号', 'OPL编号', 'OPL课程名称', 'OPL分类', '提报部门', '班组/工段', '提报人姓名',
               '年', '月', '日', '是否培训', '培训人数', '年', '月', '日']
REVIEW_HEADERS = ['终审星级', '终审积分', '评审状态']
HISTORY_HEADERS = ['提案编号', '方案名称', '部门', '评审层级', '评审人', '评审人工号', '结论', '星级', '评审日期', '意见']


def col(index):
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def clean(value):
    return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str('' if value is None else value))[:32767])


def parse_date(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def date_value(value):
    date = parse_date(value)
    return (date.replace(tzinfo=None) - dt.datetime(1899, 12, 30)).total_seconds() / 86400 if date else None


def date_parts(value):
    date = parse_date(value)
    return [date.year, date.month, date.day] if date else [None, None, None]


def number(value):
    """Preserve unknown legacy text and missing values, never fabricate zero."""
    if value in (None, ''):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else str(value)
    except (ValueError, TypeError):
        return str(value)


def final_review(item):
    # Team recommendations are not final scores. Never recalculate stored points
    # from today's settings, which may differ from the rules at approval time.
    approved = item.get('status') == 'approved'
    stars = number(item.get('stars')) if approved else None
    return [stars if stars in (1, 2, 3) else None,
            number(item.get('points')) if approved else None,
            STATUS.get(item.get('status'), item.get('status'))]


def cell(reference, value, style=0):
    if value is None:
        return f'<c r="{reference}" s="{style}"/>'
    if isinstance(value, (int, float)) and math.isfinite(value):
        return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'
    # Inline strings cannot execute formulas, including text beginning =, +, - or @.
    return f'<c r="{reference}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{clean(value)}</t></is></c>'


def worksheet(title, headers, rows, widths, date_columns=(), numeric_columns=(),
              money_columns=(), text_columns=(), groups=(), metadata=False, datetime_columns=()):
    header_row = 3 if groups or metadata else 2
    end = len(rows) + header_row
    merges = [f'A1:{col(len(headers))}1']
    xml = f'<worksheet xmlns="{NS}"><sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
    xml += f'<dimension ref="A1:{col(len(headers))}{end}"/>'
    xml += f'<sheetViews><sheetView workbookViewId="0" showGridLines="0"><pane xSplit="2" ySplit="{header_row}" topLeftCell="C{header_row+1}" activePane="bottomRight" state="frozen"/></sheetView></sheetViews>'
    xml += '<sheetFormatPr defaultRowHeight="34"/><cols>'
    xml += ''.join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>' for i, width in enumerate(widths, 1))
    xml += '</cols><sheetData>'
    xml += f'<row r="1" ht="46" customHeight="1">{cell("A1", title, 2)}</row>'
    if groups:
        xml += '<row r="2" ht="27" customHeight="1">'
        for first, last, label in groups:
            xml += ''.join(cell(f'{col(i)}2', label if i == first else None, 1) for i in range(first, last + 1))
            if first != last:
                merges.append(f'{col(first)}2:{col(last)}2')
        xml += '</row>'
    elif metadata:
        xml += f'<row r="2" ht="24" customHeight="1">{cell("A2", "导出时间", 3)}{cell("B2", dt.datetime.now().strftime("%Y-%m-%d %H:%M"), 3)}{cell("C2", f"共 {len(rows)} 条", 3)}</row>'
    header_height = 80 if len(headers) > 18 else 40
    xml += f'<row r="{header_row}" ht="{header_height}" customHeight="1">' + ''.join(cell(f'{col(i)}{header_row}', h, 1) for i, h in enumerate(headers, 1)) + '</row>'
    for r, row in enumerate(rows, header_row + 1):
        lines = max((sum(max(1, math.ceil(len(line) / max(8, widths[c] / 2))) for line in str(value or '').split('\n')) for c, value in enumerate(row)), default=1)
        height = min(409, max(36, 15 * lines + 8))
        xml += f'<row r="{r}" ht="{height}" customHeight="1">'
        for c, value in enumerate(row, 1):
            style = 8 if c in datetime_columns else 4 if c in date_columns else 6 if c in money_columns else 5 if c in numeric_columns else 7 if c in text_columns else 0
            if c in date_columns or c in datetime_columns:
                serial = date_value(value)
                if serial is not None:
                    value = serial
            xml += cell(f'{col(c)}{r}', value, style)
        xml += '</row>'
    xml += f'</sheetData><autoFilter ref="A{header_row}:{col(len(headers))}{end}"/>'
    xml += f'<mergeCells count="{len(merges)}">' + ''.join(f'<mergeCell ref="{ref}"/>' for ref in merges) + '</mergeCells>'
    xml += '<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/><pageSetup orientation="landscape" paperSize="9" fitToWidth="1" fitToHeight="0"/></worksheet>'
    return xml


def ledger_row(index, item, states):
    kind = item.get('type')
    no = item.get('sourceNo') or item.get('id')
    department = organization.department_name(item, states)
    team = item.get('team') or item.get('section')
    author = item.get('firstAuthor')
    if kind in ('six', 'lean'):
        six = kind == 'six'
        discovered = item.get('discoverDate') if six else item.get('submittedAt')
        accepted = {'approved': '是', 'rejected': '否'}.get(item.get('status'), '待评审')
        categories = item.get('categories') or []
        category = '、'.join(str(x) for x in categories) if isinstance(categories, list) else categories
        row = [index, no, department, team or item.get('position'),
               (item.get('discoverer') or author) if six else author,
               *date_parts(discovered)[:2], item.get('problem') or item.get('beforeText') or item.get('title'),
               category if six else item.get('projectType'), accepted,
               item.get('rejectReason') if item.get('status') == 'rejected' else None,
               item.get('solvingUnit'), item.get('solver'), item.get('measures') or item.get('afterText'),
               item.get('improveDate'), number(item.get('savedAmount')), item.get('savingRemark'), number(item.get('rewardAmount'))]
    elif kind == 'tool':
        row = [index, no, item.get('title'), item.get('toolCategory'), team, author, item.get('submittedAt'), department]
    elif kind == 'opl':
        category = item.get('oplCategory')
        if category == '其他' and item.get('otherCategory'):
            category += '：' + str(item['otherCategory'])
        # Date alone may be a planned session. A completed evaluation is evidence
        # of training; free-text trainees may name a whole team, not a head count.
        trained = '是' if item.get('trainingEvaluation') and item.get('trainingDate') else None
        row = [index, no, item.get('title'), category, department, team, author,
               *date_parts(item.get('submittedAt')), trained, number(item.get('trainingCount')),
               *date_parts(item.get('trainingDate'))]
    else:
        raise ValueError('不支持的台账类型')
    return row + final_review(item)


def build(items, states):
    sheets, history_rows = [], []
    for kind, label in TYPES.items():
        selected = [item for item in items if item.get('type') == kind]
        if not selected:
            continue
        rows = [ledger_row(i, item, states) for i, item in enumerate(selected, 1)]
        departments = {organization.department_name(item, states) for item in selected}
        department = next(iter(departments)) if len(departments) == 1 else '多部门'
        years = {date_parts(item.get('submittedAt'))[0] for item in selected}
        year = f'{next(iter(years))}年' if len(years) == 1 and None not in years else ''
        title = f'{department}{year}{label}统计台账'
        if kind in ('lean', 'six'):
            headers = list(SIX_HEADERS)
            if kind == 'lean':
                for index, value in {1:'提案编号', 4:'提报人姓名', 5:'提报年份', 6:'提报月份', 7:'提案问题简述', 8:'提案类别'}.items():
                    headers[index] = value
            sheet = worksheet(title, headers + REVIEW_HEADERS, rows,
                              [8, 32, 20, 18, 18, 12, 12, 65, 18, 12, 28, 20, 20, 64, 16, 28, 28, 18, 12, 12, 22],
                              date_columns=(15,), numeric_columns=(1, 6, 7, 19, 20), money_columns=(16, 18), text_columns=(2,))
        elif kind == 'tool':
            sheet = worksheet(title, TOOL_HEADERS + REVIEW_HEADERS, rows,
                              [8, 32, 44, 20, 20, 22, 16, 20, 12, 12, 22],
                              date_columns=(7,), numeric_columns=(1, 9, 10), text_columns=(2,),
                              groups=((1, 4, '基本信息'), (5, 6, '人员信息'), (7, 7, '提报日期'), (8, 8, '部门信息'), (9, 11, '评审结果')))
        else:
            sheet = worksheet(title, OPL_HEADERS + REVIEW_HEADERS, rows,
                              [8, 34, 44, 20, 20, 20, 22, 10, 8, 8, 12, 12, 10, 8, 8, 12, 12, 22],
                              numeric_columns=(1, 8, 9, 10, 12, 13, 14, 15, 16, 17), text_columns=(2,),
                              groups=((1, 4, '基本信息'), (5, 7, '人员信息'), (8, 10, '提报日期'), (11, 12, '状态信息'), (13, 15, '培训日期'), (16, 18, '评审结果')))
        sheets.append((label + '台账', sheet))
    # Keep interim, rejected and final reviews separately as the existing audit trail.
    for item in items:
        if item.get('type') not in TYPES:
            raise ValueError('不支持的台账类型')
        for h in (item.get('reviewHistory') or []):
            if not isinstance(h, dict):
                continue
            history_rows.append([item.get('sourceNo') or item.get('id'), item.get('title'), organization.department_name(item, states),
                                 h.get('levelLabel') or h.get('level'), h.get('reviewer'), h.get('reviewerWorkNo'),
                                 {'approve': '通过', 'reject': '驳回', 'resubmit': '重新提交', 'recommend': '推荐公司审核'}.get(h.get('decision'), h.get('decision')),
                                 number(h.get('stars')), h.get('time'), h.get('comment')])
    sheets.append(('评审记录', worksheet('提案评审记录', HISTORY_HEADERS, history_rows,
                  [32, 38, 20, 18, 16, 16, 16, 8, 26, 48], numeric_columns=(8,), text_columns=(1, 6), metadata=True, datetime_columns=(9,))))
    styles = f'<styleSheet xmlns="{NS}"><numFmts count="3"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/><numFmt numFmtId="165" formatCode="#,##0.00"/><numFmt numFmtId="166" formatCode="yyyy-mm-dd hh:mm"/></numFmts>'
    styles += '<fonts count="4"><font><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><sz val="18"/><name val="Microsoft YaHei"/></font><font><sz val="10"/><color rgb="FF5F666B"/><name val="Microsoft YaHei"/></font></fonts>'
    styles += '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="2"><border/><border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="9">'
    for font, num in [(0, 0), (1, 0), (2, 0), (3, 0), (0, 164), (0, 1), (0, 165), (0, 49), (0, 166)]:
        align = 'center' if font in (1, 2) else 'right' if num and num != 49 else 'left'
        border = 0 if font in (2, 3) else 1
        wrap = 0 if font == 2 else 1
        styles += f'<xf numFmtId="{num}" fontId="{font}" fillId="0" borderId="{border}" xfId="0" applyAlignment="1" applyNumberFormat="1"><alignment vertical="center" horizontal="{align}" wrapText="{wrap}"/></xf>'
    styles += '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        overrides = ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets) + 1))
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' + overrides + '</Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        names = ''.join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1))
        print_titles = ''.join(f'<definedName name="_xlnm.Print_Titles" localSheetId="{i}">\'{name}\'!$1:${3 if name in ("工具开发台账", "OPL单点课台账", "评审记录") else 2}</definedName>' for i, (name, _) in enumerate(sheets))
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{names}</sheets><definedNames>{print_titles}</definedNames></workbook>')
        relationships = ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + relationships + f'<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml', styles)
        for i, (_, sheet) in enumerate(sheets, 1):
            z.writestr(f'xl/worksheets/sheet{i}.xml', sheet)
    return buffer.getvalue()
