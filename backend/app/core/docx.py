"""生成简单的 Word 文件（.docx）：只用标准库（zip + XML），不依赖第三方包。

支持标题（居中的文档标题和两级条款标题）、段落（加粗的片段）、列表项和表格（可以有表头），
A4 纸、宋体，页脚居中显示"第 X 页 共 Y 页"。合同导出用（设计文档 §34.4）。
"""

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.sax.saxutils import escape

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml"
_FONT = "宋体"
_LATIN = "Times New Roman"
# A4（缇）：宽 21 厘米、高 29.7 厘米；上下边距 2.54 厘米，左右 3.17 厘米。
_PAGE = '<w:pgSz w:w="11906" w:h="16838"/>'
_MARGIN = (
    '<w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800"'
    ' w:header="851" w:footer="992" w:gutter="0"/>'
)
_TABLE_WIDTH = 8306  # 页面宽度减去左右边距


@dataclass(frozen=True)
class Span:
    text: str
    bold: bool = False


@dataclass
class Item:
    """文档里的一项：kind 是 title（文档标题）、heading（level 1–2）、paragraph、bullet、table。"""

    kind: str
    spans: list[Span] = field(default_factory=list)
    level: int = 1
    rows: list[list[str]] = field(default_factory=list)
    header: bool = False


def _run(span: Span, *, size: int | None = None) -> str:
    props = []
    if span.bold:
        props.append("<w:b/><w:bCs/>")
    if size is not None:
        props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    parts = span.text.split("\n")
    texts = "<w:br/>".join(f'<w:t xml:space="preserve">{escape(p)}</w:t>' for p in parts)
    return f"<w:r>{rpr}{texts}</w:r>"


def _paragraph(
    spans: Sequence[Span],
    *,
    style: str | None = None,
    align: str | None = None,
    indent: str = "",
    size: int | None = None,
) -> str:
    props = []
    if style:
        props.append(f'<w:pStyle w:val="{style}"/>')
    if indent:
        props.append(indent)
    if align:
        props.append(f'<w:jc w:val="{align}"/>')
    ppr = f"<w:pPr>{''.join(props)}</w:pPr>" if props else ""
    return f"<w:p>{ppr}{''.join(_run(s, size=size) for s in spans)}</w:p>"


def _table(rows: list[list[str]], header: bool) -> str:
    if not rows:
        return ""
    columns = max(len(r) for r in rows)
    width = _TABLE_WIDTH // max(columns, 1)
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(columns))
    body = []
    for index, row in enumerate(rows):
        bold = header and index == 0
        cells = []
        for value in row + [""] * (columns - len(row)):
            cells.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
                + _paragraph([Span(value, bold=bold)], align="center" if bold else None)
                + "</w:tc>"
            )
        repeat = "<w:trPr><w:tblHeader/></w:trPr>" if bold else ""
        body.append(f"<w:tr>{repeat}{''.join(cells)}</w:tr>")
    return (
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
        f'<w:tblW w:w="{_TABLE_WIDTH}" w:type="dxa"/><w:jc w:val="center"/></w:tblPr>'
        f"<w:tblGrid>{grid}</w:tblGrid>{''.join(body)}</w:tbl>" + _paragraph([Span("")])
        # 表格后面空一行，避免和下一段贴在一起。
    )


def _body(items: Sequence[Item]) -> str:
    out = []
    for item in items:
        if item.kind == "title":
            out.append(_paragraph(item.spans, style="Title", align="center"))
        elif item.kind == "heading":
            out.append(_paragraph(item.spans, style="Heading1" if item.level <= 1 else "Heading2"))
        elif item.kind == "bullet":
            hanging = '<w:ind w:left="420" w:hanging="420"/>'
            out.append(_paragraph([Span("•\t"), *item.spans], indent=hanging))
        elif item.kind == "table":
            out.append(_table(item.rows, item.header))
        else:
            out.append(_paragraph(item.spans))
    return "".join(out)


def _document(items: Sequence[Item]) -> str:
    section = (
        f'<w:sectPr><w:footerReference w:type="default" r:id="rIdFooter"/>{_PAGE}{_MARGIN}'
        "</w:sectPr>"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body>{_body(items)}{section}</w:body>'
        "</w:document>"
    )


def _fonts(size: int) -> str:
    return (
        f'<w:rFonts w:ascii="{_LATIN}" w:hAnsi="{_LATIN}" w:eastAsia="{_FONT}" w:cs="{_LATIN}"/>'
        f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    )


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles xmlns:w="{_W}">'
    f"<w:docDefaults><w:rPrDefault><w:rPr>{_fonts(24)}"
    '<w:lang w:val="en-US" w:eastAsia="zh-CN"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="360" w:lineRule="auto"/>'
    "</w:pPr></w:pPrDefault></w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    '<w:pPr><w:jc w:val="both"/></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
    '<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="240" w:after="360"/>'
    f'<w:jc w:val="center"/></w:pPr><w:rPr><w:b/><w:bCs/>{_fonts(36)}</w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
    '<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/>'
    f'<w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:bCs/>{_fonts(28)}</w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>'
    '<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="120" w:after="60"/>'
    f'<w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:bCs/>{_fonts(24)}</w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Footer"><w:name w:val="footer"/>'
    f'<w:basedOn w:val="Normal"/><w:pPr><w:jc w:val="center"/></w:pPr><w:rPr>{_fonts(18)}'
    "</w:rPr></w:style>"
    '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr>'
    "<w:tblBorders>"
    + "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    + '</w:tblBorders><w:tblCellMar><w:left w:w="108" w:type="dxa"/>'
    '<w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
    "</w:styles>"
)


def _field(code: str) -> str:
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> {code} </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


_FOOTER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:ftr xmlns:w="{_W}" xmlns:r="{_R}"><w:p><w:pPr><w:pStyle w:val="Footer"/>'
    '<w:jc w:val="center"/></w:pPr>'
    '<w:r><w:t xml:space="preserve">第 </w:t></w:r>'
    + _field("PAGE")
    + '<w:r><w:t xml:space="preserve"> 页 共 </w:t></w:r>'
    + _field("NUMPAGES")
    + '<w:r><w:t xml:space="preserve"> 页</w:t></w:r></w:p></w:ftr>'
)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Types xmlns="{_CT}">'
    '<Default Extension="rels"'
    ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/word/document.xml" ContentType="{_MAIN}.document.main+xml"/>'
    f'<Override PartName="/word/styles.xml" ContentType="{_MAIN}.styles+xml"/>'
    f'<Override PartName="/word/footer1.xml" ContentType="{_MAIN}.footer+xml"/>'
    '<Override PartName="/docProps/core.xml"'
    ' ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '<Override PartName="/docProps/app.xml"'
    ' ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
    "</Types>"
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{_PKG}">'
    f'<Relationship Id="rId1" Type="{_DOC}/officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/'
    'metadata/core-properties" Target="docProps/core.xml"/>'
    f'<Relationship Id="rId3" Type="{_DOC}/extended-properties" Target="docProps/app.xml"/>'
    "</Relationships>"
)

_DOCUMENT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{_PKG}">'
    f'<Relationship Id="rIdStyles" Type="{_DOC}/styles" Target="styles.xml"/>'
    f'<Relationship Id="rIdFooter" Type="{_DOC}/footer" Target="footer1.xml"/>'
    "</Relationships>"
)

_APP = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
    "<Application>EDP</Application></Properties>"
)


def _core(title: str, now: datetime) -> str:
    stamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:dcterms="http://purl.org/dc/terms/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(title)}</dc:title><dc:creator>EDP</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def build(items: Sequence[Item], *, title: str = "", now: datetime | None = None) -> bytes:
    """生成 .docx 文件的内容。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", _DOCUMENT_RELS)
        archive.writestr("word/document.xml", _document(items))
        archive.writestr("word/styles.xml", _STYLES)
        archive.writestr("word/footer1.xml", _FOOTER)
        archive.writestr("docProps/core.xml", _core(title, now or datetime.now(UTC)))
        archive.writestr("docProps/app.xml", _APP)
    return buffer.getvalue()
