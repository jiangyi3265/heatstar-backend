#!/usr/bin/env python3
"""把 Word 类提案导出成对应申报表版式（.docx），一案一页。

精益提案、工具开发和 OPL 分别使用对应字段生成归档表。
六源改善固定使用厂方 PPT 模板，不允许进入 Word 导出。
只用标准库手写 OOXML：zipfile 打包，图片嵌进 word/media 并挂关系。
版式参数取自甲方原件：A4 纵向，7 列表格，列宽 617/1554/1128/1128/1129/1107/2562 dxa。
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


COLUMNS = [617, 1554, 1128, 1128, 1129, 1107, 2562]
PAGE = dict(w=11907, h=16839, top=1431, right=1336, bottom=720, left=1339)
EMU_PER_PX = 9525
CONTENT_WIDTH_EMU = 5600000  # 表格内可用宽度，图片按此缩放
STATUS_TEXT = {
    "pending_team": "班组长审核中",
    "pending_review": "评审小组审核中",
    "pending_company": "公司审核中",
    "pending_section": "工段长审核中",
    "pending_factory": "厂长审核中",
    "approved": "已采纳",
    "rejected": "已驳回",
}


def _t(value) -> str:
    return escape("" if value is None else str(value))


def _run(text, *, bold=False, size=21, font="仿宋_GB2312", align=None):
    """一个文字段落。size 单位是半磅，21 = 10.5pt。"""
    props = f'<w:rFonts w:ascii="{font}" w:eastAsia="{font}" w:hAnsi="{font}"/>'
    if bold:
        props += "<w:b/>"
    props += f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    justify = f'<w:jc w:val="{align}"/>' if align else ""
    lines = str(text or "").split("\n")
    body = ""
    for index, line in enumerate(lines):
        brk = "<w:br/>" if index else ""
        body += f'<w:r><w:rPr>{props}</w:rPr>{brk}<w:t xml:space="preserve">{_t(line)}</w:t></w:r>'
    return f'<w:p><w:pPr>{justify}<w:spacing w:line="300" w:lineRule="auto"/><w:rPr>{props}</w:rPr></w:pPr>{body}</w:p>'


def _vertical_label(text, size=24):
    """左侧竖排的「申报栏 / 审签栏」。"""
    spaced = "\n".join(list(str(text)))
    return _run(spaced, bold=True, size=size, font="黑体", align="center")


def _cell(content, *, width, span=1, merge=None, valign="center", shade=None):
    props = f'<w:tcW w:w="{width}" w:type="dxa"/>'
    if span > 1:
        props += f'<w:gridSpan w:val="{span}"/>'
    if merge:
        props += f'<w:vMerge w:val="restart"/>' if merge == "restart" else "<w:vMerge/>"
    props += f'<w:vAlign w:val="{valign}"/>'
    if shade:
        props += f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>'
    return f"<w:tc><w:tcPr>{props}</w:tcPr>{content or _run('')}</w:tc>"


def _row(cells, *, height=0):
    trpr = f'<w:trPr><w:trHeight w:val="{height}"/></w:trPr>' if height else ""
    return f"<w:tr>{trpr}{''.join(cells)}</w:tr>"


def _table(rows):
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in COLUMNS)
    borders = (
        "<w:tblBorders>"
        + "".join(
            f'<w:{edge} w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
            for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        + "</w:tblBorders>"
    )
    props = (
        f'<w:tblPr><w:tblW w:w="{sum(COLUMNS)}" w:type="dxa"/>'
        '<w:jc w:val="center"/>' + borders +
        '<w:tblCellMar><w:top w:w="60" w:type="dxa"/><w:left w:w="80" w:type="dxa"/>'
        '<w:bottom w:w="60" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tblCellMar></w:tblPr>'
    )
    return f"<w:tbl>{props}<w:tblGrid>{grid}</w:tblGrid>{''.join(rows)}</w:tbl>"


def image_size(data: bytes) -> tuple[int, int]:
    """读出图片像素尺寸，用于按比例缩放。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if data[:2] == b"\xff\xd8":
        index = 2
        while index < len(data) - 9:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return int(width), int(height)
            if marker in (0xD8, 0xD9):
                index += 2
                continue
            length = struct.unpack(">H", data[index + 2:index + 4])[0]
            index += 2 + length
    return 800, 600


def _drawing(rid: str, doc_id: int, px_w: int, px_h: int, max_emu: int) -> str:
    width = px_w * EMU_PER_PX
    height = px_h * EMU_PER_PX
    if width > max_emu:
        height = int(height * max_emu / width)
        width = max_emu
    max_height = 2400000
    if height > max_height:
        width = int(width * max_height / height)
        height = max_height
    return (
        f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:drawing>'
        f'<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{width}" cy="{height}"/><wp:docPr id="{doc_id}" name="图片{doc_id}"/>'
        f'<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:nvPicPr><pic:cNvPr id="{doc_id}" name="图片{doc_id}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{width}" cy="{height}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        f"</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


class ImageBag:
    """收集要嵌入的图片，分配关系 id。"""

    def __init__(self, uploads: Path):
        self.uploads = Path(uploads)
        self.items: list[tuple[str, str, bytes]] = []  # (rid, filename, data)
        self._by_name: dict[str, str] = {}
        self._counter = 0

    def add(self, url: str) -> tuple[str, int, int] | None:
        name = Path(str(url or "").split("?")[0]).name
        if not name:
            return None
        path = (self.uploads / name).resolve()
        if self.uploads.resolve() not in path.parents or not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if name in self._by_name:
            rid = self._by_name[name]
        else:
            self._counter += 1
            rid = f"rIdImg{self._counter}"
            self._by_name[name] = rid
            self.items.append((rid, name, data))
        width, height = image_size(data)
        return rid, width, height


def _photo_cell(bag: ImageBag, urls, width, span, doc_ids, *, label="", limit=2) -> str:
    parts = []
    selected = list(urls or [])
    if limit:
        selected = selected[:limit]
    for url in selected:
        found = bag.add(url)
        if not found:
            continue
        rid, px_w, px_h = found
        doc_ids[0] += 1
        parts.append(_drawing(rid, doc_ids[0], px_w, px_h, CONTENT_WIDTH_EMU // max(1, span // 2 or 1)))
    if not parts:
        parts.append(_run("（未上传照片）", size=18, align="center"))
    if label:
        parts.insert(0, _run(label, bold=True, size=18, align="center"))
    return _cell("".join(parts), width=width, span=span)


def _w(*indexes) -> int:
    return sum(COLUMNS[i] for i in indexes)


def _lean_form(item: dict, bag: ImageBag, doc_ids: list) -> str:
    """精益创意提案申报表。"""
    rows = [
        _row([
            _cell(_vertical_label("申报栏"), width=COLUMNS[0], merge="restart"),
            _cell(_run("提案名称", bold=True), width=COLUMNS[1]),
            _cell(_run(item.get("title")), width=_w(2, 3, 4)  , span=3),
            _cell(_run("日期", bold=True), width=COLUMNS[5]),
            _cell(_run(str(item.get("submittedAt") or "")[:10]), width=COLUMNS[6]),
        ], height=560),
        _row([
            _cell(_run(""), width=COLUMNS[0], merge="continue"),
            _cell(_run("部门", bold=True), width=COLUMNS[1]),
            _cell(_run(item.get("team")), width=COLUMNS[2]),
            _cell(_run("项目类型", bold=True), width=COLUMNS[3]),
            _cell(_run(item.get("projectType")), width=COLUMNS[4]),
            _cell(_run("改善团队", bold=True), width=COLUMNS[5]),
            _cell(_run("、".join(x for x in [item.get("firstAuthor"), item.get("members")] if x)), width=COLUMNS[6]),
        ], height=560),
        _row([
            _cell(_run(""), width=COLUMNS[0], merge="continue"),
            _cell(_run("改善说明", bold=True), width=COLUMNS[1], merge="restart"),
            _cell(_run("改善前", bold=True, align="center"), width=_w(2, 3, 4), span=3),
            _cell(_run("改善后", bold=True, align="center"), width=_w(5, 6), span=2),
        ], height=420),
        _row([
            _cell(_run(""), width=COLUMNS[0], merge="continue"),
            _cell(_run(""), width=COLUMNS[1], merge="continue"),
            _cell(_run(item.get("beforeText") or item.get("problem")), width=_w(2, 3, 4), span=3, valign="top"),
            _cell(_run(item.get("afterText") or item.get("measures")), width=_w(5, 6), span=2, valign="top"),
        ], height=1800),
        _row([
            _cell(_run(""), width=COLUMNS[0], merge="continue"),
            _cell(_run("图片或\n数据说明", bold=True), width=COLUMNS[1]),
            _photo_cell(bag, item.get("beforePhotos"), _w(2, 3, 4), 3, doc_ids, label="改善前"),
            _photo_cell(bag, item.get("afterPhotos"), _w(5, 6), 2, doc_ids, label="改善后"),
        ], height=2600),
        _row([
            _cell(_vertical_label("审签栏"), width=COLUMNS[0], merge="restart"),
            _cell(_run("成果鉴定", bold=True), width=COLUMNS[1]),
            _cell(
                _run("（阐述精益价值、改善程度、解决问题重要性、应用范围、拟评定奖励等级）", size=18)
                + _run(item.get("reviewComment") or "")
                + _run(_award_line(item), bold=True),
                width=_w(2, 3, 4, 5, 6), span=5, valign="top",
            ),
        ], height=1600),
        _row([
            _cell(_run(""), width=COLUMNS[0], merge="continue"),
            _cell(_run("单位意见", bold=True), width=COLUMNS[1]),
            _cell(_run(_sign_line(item)), width=_w(2, 3, 4, 5, 6), span=5, valign="top"),
        ], height=1200),
    ]
    header = (
        _run("编号：" + str(item.get("sourceNo") or item.get("id") or ""), size=21)
        + _run("精益创意提案申报表", bold=True, size=43, font="黑体", align="center")
        + _run("日期：" + str(item.get("submittedAt") or "")[:10], size=21, align="right")
    )
    return header + _table(rows)


def _tool_form(item: dict, bag: ImageBag, doc_ids: list) -> str:
    """热处理厂工具开发归档表。"""
    rows = [
        _row([
            _cell(_run("工具名称", bold=True), width=_w(0, 1), span=2),
            _cell(_run(item.get("title")), width=_w(2, 3, 4), span=3),
            _cell(_run("工具类别", bold=True), width=COLUMNS[5]),
            _cell(_run(item.get("toolCategory")), width=COLUMNS[6]),
        ], height=560),
        _row([
            _cell(_run("部门/班组", bold=True), width=_w(0, 1), span=2),
            _cell(_run(" / ".join(x for x in [item.get("section"), item.get("team")] if x)), width=_w(2, 3), span=2),
            _cell(_run("开发人员", bold=True), width=COLUMNS[4]),
            _cell(_run(item.get("firstAuthor")), width=_w(5, 6), span=2),
        ], height=560),
        _row([
            _cell(_run("开发日期", bold=True), width=_w(0, 1), span=2),
            _cell(_run(item.get("developmentDate")), width=_w(2, 3), span=2),
            _cell(_run("使用范围", bold=True), width=COLUMNS[4]),
            _cell(_run(item.get("usageScope")), width=_w(5, 6), span=2),
        ], height=560),
        _row([
            _cell(_run("设计思路说明", bold=True), width=_w(0, 1), span=2),
            _cell(_run(item.get("designIdea") or item.get("beforeText")), width=_w(2, 3, 4, 5, 6), span=5, valign="top"),
        ], height=1300),
        _row([
            _cell(_run("开发/设计照片", bold=True, align="center"), width=_w(0, 1), span=2),
            _photo_cell(bag, item.get("beforePhotos"), _w(2, 3, 4, 5, 6), 5, doc_ids),
        ], height=2200),
        _row([
            _cell(_run("运用效果说明", bold=True), width=_w(0, 1), span=2),
            _cell(_run(item.get("applicationEffect") or item.get("afterText")), width=_w(2, 3, 4, 5, 6), span=5, valign="top"),
        ], height=1300),
        _row([
            _cell(_run("成品/应用照片", bold=True, align="center"), width=_w(0, 1), span=2),
            _photo_cell(bag, item.get("afterPhotos"), _w(2, 3, 4, 5, 6), 5, doc_ids),
        ], height=2200),
        _row([
            _cell(_run("审核记录", bold=True), width=_w(0, 1), span=2),
            _cell(_run(_award_line(item)) + _run(_sign_line(item)), width=_w(2, 3, 4, 5, 6), span=5, valign="top"),
        ], height=900),
    ]
    header = (
        _run("编号：" + str(item.get("sourceNo") or item.get("id") or ""), size=21)
        + _run(f"{item.get('department') or '热处理厂'}工具开发", bold=True, size=43, font="黑体", align="center")
    )
    return header + _table(rows)


def _opl_steps(item: dict) -> list[dict]:
    raw_steps = item.get("oplSteps")
    steps = []
    if isinstance(raw_steps, list):
        for raw in raw_steps:
            if not isinstance(raw, dict):
                continue
            photos = raw.get("photos") if isinstance(raw.get("photos"), list) else []
            photo = str(raw.get("photo") or (photos[0] if photos else "") or "")
            text = str(raw.get("text") or raw.get("description") or "")
            if photo or text:
                steps.append({"photo": photo, "text": text})
    if steps:
        return steps

    photos = [str(url) for url in (item.get("beforePhotos") or []) if url]
    content = str(item.get("contentText") or item.get("beforeText") or "")
    if photos:
        return [
            {"photo": photo, "text": content if index == 0 else ""}
            for index, photo in enumerate(photos)
        ]
    return [{"photo": "", "text": content}] if content else []


def _opl_form(item: dict, bag: ImageBag, doc_ids: list) -> str:
    """热处理厂 OPL 单点课程归档表。"""
    category = item.get("oplCategory") or ""
    if category == "其他" and item.get("otherCategory"):
        category = f"其他：{item.get('otherCategory')}"
    rows = [
        _row([
            _cell(_run("单位", bold=True), width=_w(0, 1), span=2),
            _cell(_run(item.get('department') or '热处理厂'), width=_w(2, 3), span=2),
            _cell(_run("编号", bold=True), width=COLUMNS[4]),
            _cell(_run(item.get("sourceNo") or item.get("id")), width=_w(5, 6), span=2),
        ], height=520),
        _row([
            _cell(_run("名称", bold=True), width=_w(0, 1), span=2),
            _cell(_run(item.get("title")), width=_w(2, 3, 4), span=3),
            _cell(_run("日期", bold=True), width=COLUMNS[5]),
            _cell(_run(item.get("oplDate")), width=COLUMNS[6]),
        ], height=520),
        _row([
            _cell(_run("编者", bold=True), width=_w(0, 1), span=2),
            _cell(_run(item.get("firstAuthor")), width=COLUMNS[2]),
            _cell(_run("部门/班组", bold=True), width=COLUMNS[3]),
            _cell(_run(" / ".join(x for x in [item.get("section"), item.get("team")] if x)), width=COLUMNS[4]),
            _cell(_run("岗位", bold=True), width=COLUMNS[5]),
            _cell(_run(item.get("position")), width=COLUMNS[6]),
        ], height=520),
        _row([
            _cell(_run("类别", bold=True), width=_w(0, 1), span=2),
            _cell(_run(category), width=_w(2, 3, 4, 5, 6), span=5),
        ], height=520),
    ]
    steps = _opl_steps(item)
    if not steps:
        steps = [{"photo": "", "text": ""}]
    for index, step in enumerate(steps, 1):
        rows.extend([
            _row([
                _cell(_run(f"步骤 {index} 说明", bold=True), width=_w(0, 1), span=2),
                _cell(_run(step.get("text") or "（未填写本步骤说明）"), width=_w(2, 3, 4, 5, 6), span=5, valign="top"),
            ], height=1200),
            _row([
                _cell(_run(f"步骤 {index} 图片", bold=True, align="center"), width=_w(0, 1), span=2),
                _photo_cell(bag, [step.get("photo")] if step.get("photo") else [], _w(2, 3, 4, 5, 6), 5, doc_ids, limit=1),
            ], height=2400),
        ])
    rows.extend([
        _row([
            _cell(_run("讲师", bold=True), width=_w(0, 1), span=2),
            _cell(_run(item.get("lecturer")), width=COLUMNS[2]),
            _cell(_run("培训日期", bold=True), width=COLUMNS[3]),
            _cell(_run(item.get("trainingDate")), width=COLUMNS[4]),
            _cell(_run("总体评价", bold=True), width=COLUMNS[5]),
            _cell(_run(item.get("trainingEvaluation")), width=COLUMNS[6]),
        ], height=620),
        _row([
            _cell(_run("受训人员", bold=True), width=_w(0, 1), span=2),
            _cell(_run(item.get("trainees")), width=_w(2, 3, 4, 5, 6), span=5),
        ], height=620),
        _row([
            _cell(_run("审核记录", bold=True), width=_w(0, 1), span=2),
            _cell(_run(_award_line(item)) + _run(_sign_line(item)), width=_w(2, 3, 4, 5, 6), span=5, valign="top"),
        ], height=900),
    ])
    return _run(f"{item.get('department') or '热处理厂'} OPL 单点课程", bold=True, size=43, font="黑体", align="center") + _table(rows)


def _award_line(item: dict) -> str:
    status = STATUS_TEXT.get(str(item.get("status")), "")
    stars = int(item.get("stars") or 0)
    points = item.get("points") or 0
    bits = [f"审核状态：{status}"] if status else []
    if stars:
        bits.append(f"评定星级：{stars} 星")
    if points:
        bits.append(f"积分：{points} 分")
    return "　".join(bits)


def _sign_line(item: dict) -> str:
    history = item.get("reviewHistory") or []
    if not history:
        return ""
    return "\n".join(
        f"{row.get('levelLabel', '')}：{row.get('reviewer', '')}　{row.get('time', '')}"
        for row in history
        if isinstance(row, dict)
    )


def build(proposals: list, uploads_dir) -> bytes:
    if any(isinstance(item, dict) and item.get("type") == "six" for item in proposals):
        raise ValueError("六源改善固定使用 PPT 模板，请勿导出为 Word")
    bag = ImageBag(Path(uploads_dir))
    doc_ids = [100]
    blocks = []
    for index, item in enumerate(proposals):
        if not isinstance(item, dict):
            continue
        forms = {"tool": _tool_form, "opl": _opl_form}
        body = forms.get(item.get("type"), _lean_form)(item, bag, doc_ids)
        if index:
            blocks.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        blocks.append(body)
    if not blocks:
        blocks.append(_run("所选范围内没有提案。", align="center"))

    sect = (
        f'<w:sectPr><w:pgSz w:w="{PAGE["w"]}" w:h="{PAGE["h"]}"/>'
        f'<w:pgMar w:top="{PAGE["top"]}" w:right="{PAGE["right"]}" w:bottom="{PAGE["bottom"]}" '
        f'w:left="{PAGE["left"]}" w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>'
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        f"<w:body>{''.join(blocks)}{sect}</w:body></w:document>"
    )

    rels = ['<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>']
    for rid, name, _ in bag.items:
        rels.append(
            f'<Relationship Id="{rid}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            f'Target="media/{name}"/>'
        )
    document_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels) + "</Relationships>"
    )

    defaults = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "gif": "image/gif"}
    extensions = {Path(name).suffix.lstrip(".").lower() for _, name, _ in bag.items}
    default_tags = '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
    for ext in sorted(extensions):
        default_tags += f'<Default Extension="{ext}" ContentType="{defaults.get(ext, "application/octet-stream")}"/>'
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        + default_tags +
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )

    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )

    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults><w:rPrDefault><w:rPr>'
        '<w:rFonts w:ascii="仿宋_GB2312" w:eastAsia="仿宋_GB2312" w:hAnsi="仿宋_GB2312"/>'
        '<w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr></w:rPrDefault>'
        '<w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
        "</w:docDefaults></w:styles>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
        for _, name, data in bag.items:
            archive.writestr(f"word/media/{name}", data)
    return buffer.getvalue()


def filename_for(scope: str = "") -> str:
    safe = re.sub(r"[^\w一-龥-]+", "", str(scope or "")) or "全部"
    return f"热创星提案申报表-{safe}.docx"
