#!/usr/bin/env python3
"""六源改善导出成甲方原来的「六源案例」PPT，一案一页。

做法是拿甲方给的原始 pptx 当模板填充，而不是从零画：
品牌抬头、页脚装饰、字体和坐标全部原样保留，只把文字和前后照片换掉。
模板文件：templates/six-source-template.pptx
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape


TEMPLATE = Path(__file__).resolve().parent / "templates" / "six-source-template.pptx"
SLIDE = "ppt/slides/slide1.xml"
SLIDE_RELS = "ppt/slides/_rels/slide1.xml.rels"

SIX_SOURCES = ["污染源", "清扫困难源", "故障源", "浪费源", "缺陷源", "危险源"]
CIRCLED = ["①", "②", "③", "④", "⑤", "⑥"]

# 模板里各字段对应的形状 id（由原件解析得到）
SHAPE_TITLE = "2"
SHAPE_SUBMIT_DATE = "6"
SHAPE_TEAM = "7"
SHAPE_IMPROVE_DATE = "8"
SHAPE_NAMES = "9"
SHAPE_PROBLEM = "17"
SHAPE_MEASURE = "23"
SHAPE_EFFECT = "12"
SHAPE_SIX_A = "4114"
SHAPE_SIX_B = "4115"
FLOATING_CHECKS = ("3", "5")   # 原件里浮在方框上的两个 ✅，换提案后位置会错，直接删掉
EMBED_BEFORE = "rId3"
EMBED_AFTER = "rId4"


def _find_shape(xml: str, shape_id: str):
    """返回某个形状 <p:sp> 的起止位置。"""
    for match in re.finditer(r"<p:sp>.*?</p:sp>", xml, re.S):
        block = match.group(0)
        found = re.search(r'<p:cNvPr[^>]*\bid="(\d+)"', block)
        if found and found.group(1) == shape_id:
            return match.start(), match.end(), block
    return None


def _set_text(block: str, text: str, keep: int = 0) -> str:
    """把形状里第 keep 个文字段替换成新内容，并删掉它后面的段。

    keep 用来保留「●问题描述：」这类前缀段的格式。
    """
    runs = list(re.finditer(r"<a:r>.*?</a:r>", block, re.S))
    if not runs:
        return block
    index = min(keep, len(runs) - 1)
    target = runs[index]
    replaced = re.sub(
        r"<a:t>[^<]*</a:t>", f"<a:t>{escape(text)}</a:t>", target.group(0), count=1
    )
    result = block[: target.start()] + replaced
    tail = block[target.end():]
    # 去掉本段之后的所有文字段，避免残留原提案内容
    tail = re.sub(r"<a:r>.*?</a:r>", "", tail, flags=re.S)
    return result + tail


def _replace_shape(xml: str, shape_id: str, text: str, keep: int = 0) -> str:
    found = _find_shape(xml, shape_id)
    if not found:
        return xml
    start, end, block = found
    return xml[:start] + _set_text(block, text, keep) + xml[end:]


def _toggle_six(xml: str, shape_id: str, selected) -> str:
    """把选中的六源分类的 □ 换成 ☑。

    替换前后字符数相同，所以可以按原来每段的长度切回去，不破坏排版。
    """
    found = _find_shape(xml, shape_id)
    if not found:
        return xml
    start, end, block = found
    parts = re.findall(r"<a:t>([^<]*)</a:t>", block)
    joined = "".join(parts)
    updated = joined
    for index, name in enumerate(SIX_SOURCES):
        if name in selected:
            updated = updated.replace("□" + CIRCLED[index] + name, "☑" + CIRCLED[index] + name)
    if updated == joined:
        return xml

    pieces = []
    cursor = 0
    for part in parts:
        pieces.append(updated[cursor:cursor + len(part)])
        cursor += len(part)

    counter = [0]

    def swap(match):
        value = pieces[counter[0]]
        counter[0] += 1
        return f"<a:t>{escape(value)}</a:t>"

    return xml[:start] + re.sub(r"<a:t>[^<]*</a:t>", swap, block) + xml[end:]


def _drop_shape(xml: str, shape_id: str) -> str:
    found = _find_shape(xml, shape_id)
    if not found:
        return xml
    start, end, _ = found
    return xml[:start] + xml[end:]


def _drop_pic(xml: str, embed: str) -> str:
    for match in re.finditer(r"<p:pic>.*?</p:pic>", xml, re.S):
        if f'r:embed="{embed}"' in match.group(0):
            return xml[: match.start()] + xml[match.end():]
    return xml


def _strip_tags(xml: str) -> str:
    """去掉 slide 上的自定义标记。

    模板里每页都挂着 ppt/tags 部件，而同一个 tag 部件被多页共享时
    PowerPoint 会判定文件损坏、拒绝打开。这些标记对导出内容没有意义。
    """
    return re.sub(r"<p:custDataLst>.*?</p:custDataLst>", "", xml, flags=re.S)


def _clean_rels(rels: str) -> str:
    rels = re.sub(r"<Relationship[^>]*notesSlide[^>]*/>", "", rels)
    rels = re.sub(r'<Relationship[^>]*Target="\.\./tags/[^"]*"[^>]*/>', "", rels)
    return rels


def _date(value: str) -> str:
    """模板里的日期是 2025.08.15 这种写法。"""
    text = str(value or "")[:10].replace("-", ".").replace("/", ".")
    return text


def _names(item: dict) -> str:
    people = [item.get("firstAuthor"), item.get("members")]
    return "  ".join(str(x).strip() for x in people if str(x or "").strip())


def _photo(uploads: Path, urls) -> tuple[bytes, str] | None:
    for url in (urls or []):
        name = Path(str(url or "").split("?")[0]).name
        if not name:
            continue
        path = (uploads / name).resolve()
        try:
            if uploads.resolve() not in path.parents or not path.is_file():
                continue
            return path.read_bytes(), path.suffix.lower().lstrip(".") or "jpeg"
        except OSError:
            continue
    return None


def build(proposals: list, uploads_dir, template_path=None) -> bytes:
    template = Path(template_path or TEMPLATE)
    if not template.is_file():
        raise FileNotFoundError("找不到六源案例 PPT 模板")

    uploads = Path(uploads_dir)
    source = zipfile.ZipFile(template)
    files = {name: source.read(name) for name in source.namelist() if not name.endswith("/")}
    base_slide = files[SLIDE].decode("utf-8")
    base_rels = files[SLIDE_RELS].decode("utf-8")
    source.close()

    six_items = [p for p in proposals if isinstance(p, dict) and p.get("type") == "six"]
    if not six_items:
        six_items = []

    slides = []
    media = {}
    for index, item in enumerate(six_items, start=1):
        xml = base_slide
        xml = _replace_shape(xml, SHAPE_TITLE, str(item.get("title") or ""))
        xml = _replace_shape(xml, SHAPE_SUBMIT_DATE, _date(item.get("discoverDate") or item.get("submittedAt")))
        xml = _replace_shape(xml, SHAPE_IMPROVE_DATE, _date(item.get("improveDate")))
        xml = _replace_shape(xml, SHAPE_TEAM, str(item.get("team") or ""))
        xml = _replace_shape(xml, SHAPE_NAMES, _names(item), keep=1)
        # 问题与措施保留「●问题描述：」这样的前缀段，只换正文
        xml = _replace_shape(xml, SHAPE_PROBLEM, str(item.get("problem") or item.get("beforeText") or ""), keep=2)
        xml = _replace_shape(xml, SHAPE_MEASURE, str(item.get("measures") or item.get("afterText") or ""), keep=2)
        xml = _replace_shape(xml, SHAPE_EFFECT, str(item.get("reviewComment") or ""))

        selected = set(item.get("categories") or [])
        xml = _toggle_six(xml, SHAPE_SIX_A, selected)
        xml = _toggle_six(xml, SHAPE_SIX_B, selected)
        for shape_id in FLOATING_CHECKS:
            xml = _drop_shape(xml, shape_id)

        xml = _strip_tags(xml)
        rels = _clean_rels(base_rels)

        before = _photo(uploads, item.get("beforePhotos"))
        after = _photo(uploads, item.get("afterPhotos"))
        for embed, photo, tag in ((EMBED_BEFORE, before, "before"), (EMBED_AFTER, after, "after")):
            if not photo:
                xml = _drop_pic(xml, embed)
                rels = re.sub(rf'<Relationship[^>]*Id="{embed}"[^>]*/>', "", rels)
                continue
            data, ext = photo
            name = f"p{index}-{tag}.{ext}"
            media[f"ppt/media/{name}"] = data
            rels = re.sub(
                rf'(<Relationship[^>]*Id="{embed}"[^>]*Target=")[^"]*(")',
                rf"\g<1>../media/{name}\g<2>",
                rels,
            )

        slides.append((index, xml, rels))

    if not slides:
        # 没有六源提案时给一页空白说明，避免交付一个打不开的文件
        xml = _replace_shape(base_slide, SHAPE_TITLE, "所选范围内没有六源改善提案")
        for shape_id in (SHAPE_PROBLEM, SHAPE_MEASURE, SHAPE_EFFECT, SHAPE_NAMES,
                         SHAPE_TEAM, SHAPE_SUBMIT_DATE, SHAPE_IMPROVE_DATE):
            xml = _replace_shape(xml, shape_id, "", keep=0)
        for shape_id in FLOATING_CHECKS:
            xml = _drop_shape(xml, shape_id)
        xml = _strip_tags(xml)
        rels = _clean_rels(base_rels)
        for embed in (EMBED_BEFORE, EMBED_AFTER):
            xml = _drop_pic(xml, embed)
            rels = re.sub(rf'<Relationship[^>]*Id="{embed}"[^>]*/>', "", rels)
        slides.append((1, xml, rels))

    # 组装：presentation.xml 的幻灯片清单 + 包关系 + 内容类型
    presentation = files["ppt/presentation.xml"].decode("utf-8")
    pres_rels = files["ppt/_rels/presentation.xml.rels"].decode("utf-8")
    content_types = files["[Content_Types].xml"].decode("utf-8")

    slide_ids = []
    rel_entries = []
    base_rid = 900
    for order, (index, _, _) in enumerate(slides):
        rid = f"rIdSlide{index}"
        slide_ids.append(f'<p:sldId id="{base_rid + order}" r:id="{rid}"/>')
        rel_entries.append(
            f'<Relationship Id="{rid}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            f'Target="slides/slide{index}.xml"/>'
        )

    presentation = re.sub(
        r"<p:sldIdLst>.*?</p:sldIdLst>",
        "<p:sldIdLst>" + "".join(slide_ids) + "</p:sldIdLst>",
        presentation,
        flags=re.S,
    )
    pres_rels = re.sub(r'<Relationship[^>]*Type="[^"]*/slide"[^>]*/>', "", pres_rels)
    pres_rels = pres_rels.replace("</Relationships>", "".join(rel_entries) + "</Relationships>")

    content_types = re.sub(r'<Override PartName="/ppt/slides/slide\d+\.xml"[^>]*/>', "", content_types)
    content_types = re.sub(r'<Override PartName="/ppt/notesSlides/[^"]*"[^>]*/>', "", content_types)
    content_types = re.sub(r'<Override PartName="/ppt/tags/[^"]*"[^>]*/>', "", content_types)
    overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for index, _, _ in slides
    )
    content_types = content_types.replace("</Types>", overrides + "</Types>")
    # 扩展名在 OOXML 里大小写不敏感，重复声明会让 PowerPoint 判定文件损坏
    declared = {e.lower() for e in re.findall(r'<Default Extension="([^"]+)"', content_types)}
    used_ext = {Path(name).suffix.lstrip(".").lower() for name in media}
    mimes = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "gif": "image/gif"}
    for extension in sorted(used_ext):
        if extension and extension not in declared:
            content_types = content_types.replace(
                "</Types>",
                f'<Default Extension="{extension}" ContentType="{mimes.get(extension, "application/octet-stream")}"/></Types>',
            )
            declared.add(extension)

    # 模板自带的原提案照片不应留在导出件里，只保留仍被引用的媒体
    referenced = set()
    for name, data in files.items():
        if name.endswith(".rels") and not name.startswith("ppt/slides/"):
            for target in re.findall(r'Target="([^"]+)"', data.decode("utf-8", "replace")):
                referenced.add(Path(target).name)
    for _, _, rels in slides:
        for target in re.findall(r'Target="([^"]+)"', rels):
            referenced.add(Path(target).name)

    # 文档属性里的页数跟着实际页数走
    app_xml = files.get("docProps/app.xml", b"").decode("utf-8", "replace")
    if app_xml:
        app_xml = re.sub(r"<Slides>\d+</Slides>", f"<Slides>{len(slides)}</Slides>", app_xml)
        files["docProps/app.xml"] = app_xml.encode("utf-8")

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            if name.startswith("ppt/slides/") or name.startswith("ppt/notesSlides/"):
                continue
            if name.startswith("ppt/tags/"):
                continue
            if name in ("[Content_Types].xml", "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels"):
                continue
            if name.startswith("ppt/media/") and Path(name).name not in referenced:
                continue
            archive.writestr(name, data)
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        for index, xml, rels in slides:
            archive.writestr(f"ppt/slides/slide{index}.xml", xml)
            archive.writestr(f"ppt/slides/_rels/slide{index}.xml.rels", rels)
        for name, data in media.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def count_six(proposals: list) -> int:
    return sum(1 for p in proposals if isinstance(p, dict) and p.get("type") == "six")


def filename_for(scope: str = "") -> str:
    safe = re.sub(r"[^\w一-龥-]+", "", str(scope or "")) or "全部"
    return f"热创星六源案例-{safe}.pptx"
