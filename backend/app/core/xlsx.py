"""生成简单的 Excel 文件（.xlsx）：只用标准库（zip + XML），不依赖第三方包。

支持多个工作表、加粗的表头、冻结首行、列宽，以及数据验证（选中单元格时显示的填写提示、
只能填不小于 0 的数字、从下拉列表里选）。单元格一律写成文本或数字。
"""

import io
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MAX_ROW = 1_048_576

Cell = str | int | float | None


@dataclass
class Column:
    title: str
    width: int = 14
    # 选中这一列的单元格时显示的提示（标题、内容）。
    prompt: tuple[str, str] | None = None
    # 只能填不小于 0 的数字（价格等），填错时的提示。
    decimal_error: str | None = None
    # 只能填不小于 0 的整数，填错时的提示。
    whole_error: str | None = None
    # 从下拉列表里选（例如 成品、材料），填错时提示可选的值。
    choices: tuple[str, ...] = ()


@dataclass
class Sheet:
    name: str
    columns: list[Column]
    rows: list[list[Cell]] = field(default_factory=list)
    # 数据验证覆盖到第几行（模板留出填写的空间）。
    validate_rows: int = 5000


def column_letter(index: int) -> str:
    """0 → A，25 → Z，26 → AA。"""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


def _cell(ref: str, value: Cell, style: int) -> str:
    attr = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{attr}/>' if style else ""
    if isinstance(value, bool):
        value = "是" if value else "否"
    if isinstance(value, int | float):
        return f'<c r="{ref}"{attr}><v>{value}</v></c>'
    text = escape(str(value))
    space = ' xml:space="preserve"' if text != text.strip() else ""
    return f'<c r="{ref}" t="inlineStr"{attr}><is><t{space}>{text}</t></is></c>'


def _validations(sheet: Sheet) -> str:
    items: list[str] = []
    last = min(MAX_ROW, sheet.validate_rows + 1)
    for index, column in enumerate(sheet.columns):
        letter = column_letter(index)
        area = f"{letter}2:{letter}{last}"
        prompt = ""
        if column.prompt:
            title, body = column.prompt
            prompt = (
                f' showInputMessage="1" promptTitle={quoteattr(title[:32])}'
                f" prompt={quoteattr(body[:255])}"
            )
        kind, error = (
            ("decimal", column.decimal_error)
            if column.decimal_error
            else ("whole", column.whole_error)
        )
        if error:
            items.append(
                f'<dataValidation type="{kind}" operator="greaterThanOrEqual" allowBlank="1"'
                f' showErrorMessage="1" errorTitle="格式不正确"'
                f" error={quoteattr(error[:255])}{prompt}"
                f' sqref="{area}"><formula1>0</formula1></dataValidation>'
            )
        elif column.choices:
            listed = ",".join(column.choices)
            items.append(
                f'<dataValidation type="list" allowBlank="1" showErrorMessage="1"'
                f' errorTitle="格式不正确" error={quoteattr(f"只能填：{listed}"[:255])}{prompt}'
                f' sqref="{area}"><formula1>{escape(f"{chr(34)}{listed}{chr(34)}")}</formula1>'
                "</dataValidation>"
            )
        elif prompt:
            items.append(f'<dataValidation allowBlank="1"{prompt} sqref="{area}"/>')
    if not items:
        return ""
    return f'<dataValidations count="{len(items)}">{"".join(items)}</dataValidations>'


def _worksheet(sheet: Sheet) -> str:
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{c.width}" customWidth="1"/>'
        for i, c in enumerate(sheet.columns)
    )
    header = "".join(_cell(f"{column_letter(i)}1", c.title, 1) for i, c in enumerate(sheet.columns))
    body = [f'<row r="1">{header}</row>']
    for r, row in enumerate(sheet.rows, start=2):
        cells = "".join(_cell(f"{column_letter(c)}{r}", value, 0) for c, value in enumerate(row))
        body.append(f'<row r="{r}">{cells}</row>')
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_S}">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"<cols>{cols}</cols><sheetData>{''.join(body)}</sheetData>"
        f"{_validations(sheet)}</worksheet>"
    )


_STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{_S}">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFEFF3F8"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    "</cellXfs></styleSheet>"
)


def write_workbook(sheets: list[Sheet]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        archive.writestr(
            "[Content_Types].xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="{_CT}">'
            '<Default Extension="rels" ContentType="application/'
            'vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{overrides}</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_PKG}">'
            f'<Relationship Id="rId1" Type="{_DOC}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        entries = "".join(
            f'<sheet name={quoteattr(s.name[:31])} sheetId="{i}" r:id="rId{i}"/>'
            for i, s in enumerate(sheets, start=1)
        )
        archive.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{_S}" xmlns:r="{_R}"><sheets>{entries}</sheets></workbook>',
        )
        rels = "".join(
            f'<Relationship Id="rId{i}" Type="{_DOC}/worksheet" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        style_id = len(sheets) + 1
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_PKG}">'
            f'{rels}<Relationship Id="rId{style_id}" Type="{_DOC}/styles" Target="styles.xml"/>'
            "</Relationships>",
        )
        archive.writestr("xl/styles.xml", _STYLES)
        for i, sheet in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{i}.xml", _worksheet(sheet))
    return buffer.getvalue()


XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
