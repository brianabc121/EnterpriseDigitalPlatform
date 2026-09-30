"""知识导入的文件解析（设计文档 §12.1 冷启动）：文档转成用 Markdown 标题表示层级的文本，
表格转成行（导入问答）。

- Word（.docx）：按段落样式（标题 1–6、大纲级别）识别标题，编号段落作为列表，表格按行输出；
- PDF：逐页提取文字（PDF 没有可靠的标题结构）；
- 网页（.html）：去掉导航、页脚、脚本等，h1–h6 作为标题；有 main 或 article 时只取其中的正文；
- Markdown、纯文本：原样使用（按 UTF-8 解码，失败时按 GB18030）；
- Excel（.xlsx）读取第一个工作表；CSV 按 UTF-8 或 GB18030 解码。

只用标准库解析 Office 文件（zip + XML），PDF 使用 pypdf。
"""

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

DOCUMENT_TYPES = (".docx", ".pdf", ".md", ".markdown", ".txt", ".html", ".htm")
SHEET_TYPES = (".xlsx", ".csv")
MAX_PART_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 500
ITEM_LIMIT = 50_000

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_R = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


class ParseError(Exception):
    """文件无法解析（格式不支持、已加密、内容为空等），消息直接展示给用户。"""


@dataclass
class ParsedDocument:
    title: str
    text: str
    links: list[str] = field(default_factory=list)


def suffix(filename: str) -> str:
    return PurePosixPath(filename.lower()).suffix


def stem(filename: str) -> str:
    return PurePosixPath(filename).stem.strip() or filename


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_document(filename: str, data: bytes) -> ParsedDocument:
    ext = suffix(filename)
    if ext == ".docx":
        doc = _docx(data)
    elif ext == ".pdf":
        doc = _pdf(data)
    elif ext in (".html", ".htm"):
        doc = html_to_document(decode_text(data))
    elif ext in (".md", ".markdown", ".txt"):
        text = decode_text(data).replace("\r\n", "\n").strip()
        doc = ParsedDocument(title=_first_heading(text) if ext != ".txt" else "", text=text)
    elif ext in (".doc", ".wps"):
        raise ParseError("请把 .doc 文件另存为 .docx 后再导入")
    else:
        raise ParseError("不支持这种文件格式（支持 PDF、Word、Markdown、网页和纯文本）")
    text = re.sub(r"\n{3,}", "\n\n", doc.text).strip()
    if not text:
        raise ParseError("没有从文件里读到文字（扫描件需要先做文字识别）")
    return ParsedDocument(title=(doc.title or stem(filename)).strip()[:200], text=text)


def parse_sheet(filename: str, data: bytes) -> list[list[str]]:
    ext = suffix(filename)
    if ext == ".xlsx":
        return _xlsx(data)
    if ext == ".csv":
        return list(csv.reader(io.StringIO(decode_text(data))))
    if ext == ".xls":
        raise ParseError("请把 .xls 文件另存为 .xlsx 后再导入")
    raise ParseError("问答表格支持 .xlsx 和 .csv")


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        match = _HEADING.match(line.strip())
        if match:
            return match.group(2)
    return ""


# ---- 超长文档拆成多条知识 ----


def split_long(title: str, text: str, limit: int = ITEM_LIMIT) -> list[tuple[str, str]]:
    """一条知识的正文最多 5 万字：超出时在段落处拆成几条，每条开头带上当时所在的各级标题。"""
    if len(text) <= limit:
        return [(title, text)]
    step = limit // 2
    parts: list[str] = []
    current: list[str] = []
    size = 0
    headings: list[tuple[int, str]] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        # 没有空行的超长段落按长度硬切。
        for piece in [block[i : i + step] for i in range(0, len(block), step)]:
            if current and size + len(piece) + 2 > limit:
                parts.append("\n\n".join(current))
                current = [f"{'#' * level} {name}"[:200] for level, name in headings]
                size = sum(len(h) + 2 for h in current)
            match = _HEADING.match(piece.splitlines()[0])
            if match:
                level = len(match.group(1))
                headings = [h for h in headings if h[0] < level] + [(level, match.group(2))]
            current.append(piece)
            size += len(piece) + 2
    if current:
        parts.append("\n\n".join(current))
    return [(f"{title}（第 {i} 部分）", part) for i, part in enumerate(parts, start=1)]


# ---- Word ----


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ParseError("文件已损坏或不是有效的 Office 文件") from exc
    if info.file_size > MAX_PART_BYTES:
        raise ParseError("文件内容过大")
    return archive.read(info)


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ParseError("文件已损坏或不是有效的 Office 文件") from exc


def _xml(data: bytes) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ParseError("文件已损坏或不是有效的 Office 文件") from exc


def _heading_level(name: str) -> int | None:
    """样式名对应的标题级别：Title/标题 为 0（文档标题），Heading N/标题 N 为 N。"""
    lowered = name.strip().lower()
    if lowered in ("title", "标题"):
        return 0
    match = re.fullmatch(r"(?:heading|标题)\s*(\d)", lowered)
    if match and 1 <= int(match.group(1)) <= 6:
        return int(match.group(1))
    return None


def _docx_levels(archive: zipfile.ZipFile) -> dict[str, int]:
    if "word/styles.xml" not in archive.namelist():
        return {}
    levels: dict[str, int] = {}
    for style in _xml(_read(archive, "word/styles.xml")).iter(f"{_W}style"):
        style_id = style.get(f"{_W}styleId") or ""
        name = style.find(f"{_W}name")
        level = _heading_level(name.get(f"{_W}val", "") if name is not None else "")
        if level is None:
            level = _heading_level(style_id)
        outline = style.find(f"{_W}pPr/{_W}outlineLvl")
        if level is None and outline is not None:
            level = int(outline.get(f"{_W}val", "9")) + 1
        if style_id and level is not None and level <= 6:
            levels[style_id] = level
    return levels


def _docx_title(archive: zipfile.ZipFile) -> str:
    if "docProps/core.xml" not in archive.namelist():
        return ""
    title = _xml(_read(archive, "docProps/core.xml")).find(f"{_DC}title")
    return (title.text or "").strip() if title is not None else ""


def _runs(paragraph: ET.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts).strip()


def _docx(data: bytes) -> ParsedDocument:
    with _open_zip(data) as archive:
        document = _xml(_read(archive, "word/document.xml"))
        levels = _docx_levels(archive)
        title = _docx_title(archive)
    body = document.find(f"{_W}body")
    if body is None:
        raise ParseError("文件已损坏或不是有效的 Word 文件")
    blocks: list[str] = []
    for block in body:
        if block.tag == f"{_W}p":
            text = _runs(block)
            if not text:
                continue
            style = block.find(f"{_W}pPr/{_W}pStyle")
            level = levels.get(style.get(f"{_W}val", "")) if style is not None else None
            outline = block.find(f"{_W}pPr/{_W}outlineLvl")
            if level is None and outline is not None:
                level = int(outline.get(f"{_W}val", "9")) + 1
            if level == 0:
                title = title or text
            elif level is not None and level <= 6:
                blocks.append(f"{'#' * level} {text}")
            elif block.find(f"{_W}pPr/{_W}numPr") is not None:
                blocks.append(f"- {text}")
            else:
                blocks.append(text)
        elif block.tag == f"{_W}tbl":
            rows = []
            for row in block.iter(f"{_W}tr"):
                cells = [
                    " ".join(filter(None, (_runs(p) for p in cell.iter(f"{_W}p"))))
                    for cell in row.findall(f"{_W}tc")
                ]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append("\n".join(rows))
    return ParsedDocument(title=title, text="\n\n".join(blocks))


# ---- Excel ----


def _column(ref: str) -> int:
    index = 0
    for char in ref:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    return index - 1


def _first_sheet(archive: zipfile.ZipFile) -> str:
    names = archive.namelist()
    if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
        sheet = _xml(_read(archive, "xl/workbook.xml")).find(f"{_S}sheets/{_S}sheet")
        rel_id = sheet.get(f"{_R}id") if sheet is not None else None
        for rel in _xml(_read(archive, "xl/_rels/workbook.xml.rels")).iter(f"{_PKG_R}Relationship"):
            if rel.get("Id") == rel_id:
                target = rel.get("Target", "")
                path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
                if path in names:
                    return path
    if "xl/worksheets/sheet1.xml" in names:
        return "xl/worksheets/sheet1.xml"
    raise ParseError("没有找到工作表")


def _xlsx(data: bytes) -> list[list[str]]:
    with _open_zip(data) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            for item in _xml(_read(archive, "xl/sharedStrings.xml")).iter(f"{_S}si"):
                shared.append("".join(t.text or "" for t in item.iter(f"{_S}t")))
        sheet = _xml(_read(archive, _first_sheet(archive)))
    rows: list[list[str]] = []
    for row in sheet.iter(f"{_S}row"):
        values: dict[int, str] = {}
        for cell in row.findall(f"{_S}c"):
            ref = cell.get("r") or ""
            column = _column(ref) if ref else len(values)
            kind = cell.get("t")
            value = cell.find(f"{_S}v")
            if kind == "s":
                index = int(value.text) if value is not None and value.text else -1
                text = shared[index] if 0 <= index < len(shared) else ""
            elif kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(f"{_S}t"))
            else:
                text = value.text or "" if value is not None else ""
            values[column] = text
        if values:
            rows.append([values.get(i, "") for i in range(max(values) + 1)])
    return rows


# ---- PDF ----


def _pdf(data: bytes) -> ParsedDocument:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ParseError("PDF 已加密，请先去掉密码再导入")
        pages = [(page.extract_text() or "").strip() for page in reader.pages[:MAX_PDF_PAGES]]
        title = str(reader.metadata.title or "") if reader.metadata else ""
    except ParseError:
        raise
    except (PdfReadError, ValueError, KeyError) as exc:
        raise ParseError("PDF 文件已损坏，无法读取") from exc
    return ParsedDocument(title=title, text="\n\n".join(p for p in pages if p))


# ---- 网页 ----

_SKIP = {
    "script",
    "style",
    "noscript",
    "nav",
    "footer",
    "aside",
    "form",
    "svg",
    "iframe",
    "template",
    "button",
    "select",
}
_BLOCKS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "header",
    "ul",
    "ol",
    "li",
    "table",
    "tr",
    "br",
    "blockquote",
    "pre",
    "dl",
    "dt",
    "dd",
    "hr",
}
_MAIN = {"main", "article"}


class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.main = 0
        self.in_title = False
        self.title = ""
        self.heading: int | None = None
        self.prefix = ""
        self.current: list[str] = []
        self.blocks: list[tuple[bool, str]] = []
        self.links: list[str] = []

    def flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.current)).strip()
        self.current = []
        if not text:
            self.heading, self.prefix = None, ""
            return
        if self.heading is not None:
            text = f"{'#' * self.heading} {text}"
        elif self.prefix:
            text = f"{self.prefix}{text}"
        self.blocks.append((self.main > 0, text))
        self.heading, self.prefix = None, ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
            return
        if tag == "title":
            self.in_title = True
            return
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in _MAIN:
            self.main += 1
        if re.fullmatch(r"h[1-6]", tag):
            self.flush()
            self.heading = int(tag[1])
        elif tag in _BLOCKS:
            self.flush()
            if tag == "li":
                self.prefix = "- "
        elif tag in ("td", "th"):
            self.current.append(" | " if self.current else "")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "title":
            self.in_title = False
            return
        if re.fullmatch(r"h[1-6]", tag) or tag in _BLOCKS:
            self.flush()
        if tag in _MAIN:
            self.main = max(0, self.main - 1)

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.current.append(data)


def html_to_document(html: str) -> ParsedDocument:
    """网页正文转成带 Markdown 标题的文本，同时返回页面里的链接（抓取帮助中心时使用）。"""
    parser = _Html()
    parser.feed(html)
    parser.close()
    parser.flush()
    main = [text for in_main, text in parser.blocks if in_main]
    chosen = main if sum(len(t) for t in main) >= 100 else [t for _, t in parser.blocks]
    title = re.sub(r"\s+", " ", parser.title).strip()
    if not title:
        title = next((t.lstrip("# ") for t in chosen if t.startswith("# ")), "")
    return ParsedDocument(title=title, text="\n\n".join(chosen), links=parser.links)
