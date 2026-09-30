"""导出 CSV 的小工具：带 BOM（Excel 直接打开不乱码），防止单元格被表格软件当作公式执行。"""

import csv
import io
from collections.abc import Iterable

BOM = "﻿"
# 以这些字符开头的单元格会被表格软件当作公式执行，前面加单引号。
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA) else text


def line(values: Iterable[object]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow([cell(v) for v in values])
    return buffer.getvalue()
