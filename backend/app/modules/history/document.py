"""把快照整理成可以阅读的单据（字段和明细表），比较两个版本（设计文档 §25.14）。

各业务模块的 present() 把自己的快照转成 Doc：字段是"标签 → 显示的文字"，明细表按行的 key 对齐。
比较在显示的文字上进行，所以员工、客户等引用在查看时换成名称后再比较。

新加的字段（升级前的版本里没有）不算修改：旧版本里没有的字段、列跳过。
"""

from dataclasses import dataclass, field


@dataclass
class Field:
    key: str
    label: str
    value: str


@dataclass
class Row:
    key: str
    cells: dict[str, str]


@dataclass
class Table:
    key: str
    label: str
    columns: list[tuple[str, str]]
    rows: list[Row] = field(default_factory=list)
    # 摘要里用哪一列指代一行（通常是名称）。
    title: str = "name"


@dataclass
class Doc:
    fields: list[Field] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)

    def add(self, key: str, label: str, value: str | None) -> None:
        self.fields.append(Field(key, label, value or ""))


@dataclass
class CellChange:
    key: str
    before: str
    after: str


@dataclass
class RowChange:
    key: str
    cells: list[CellChange]


@dataclass
class TableChanges:
    key: str
    added: list[str] = field(default_factory=list)
    removed: list[Row] = field(default_factory=list)
    changed: list[RowChange] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


@dataclass
class FieldChange:
    key: str
    label: str
    before: str
    after: str


@dataclass
class Changes:
    fields: list[FieldChange] = field(default_factory=list)
    tables: list[TableChanges] = field(default_factory=list)

    def empty(self) -> bool:
        return not self.fields and all(t.empty() for t in self.tables)


def compare(old: Doc | None, new: Doc) -> Changes:
    """new 相对于 old 的改动；没有旧版本时没有改动。"""
    changes = Changes()
    if old is None:
        return changes
    before = {f.key: f.value for f in old.fields}
    for f in new.fields:
        if f.key in before and before[f.key] != f.value:
            changes.fields.append(FieldChange(f.key, f.label, before[f.key], f.value))
    old_tables = {t.key: t for t in old.tables}
    for table in new.tables:
        previous = old_tables.get(table.key)
        if previous is None:
            continue
        diff = TableChanges(table.key)
        old_rows = {r.key: r for r in previous.rows}
        new_keys = {r.key for r in table.rows}
        for row in table.rows:
            was = old_rows.get(row.key)
            if was is None:
                diff.added.append(row.key)
                continue
            cells = [
                CellChange(key, was.cells[key], value)
                for key, value in row.cells.items()
                if key in was.cells and was.cells[key] != value
            ]
            if cells:
                diff.changed.append(RowChange(row.key, cells))
        diff.removed = [r for r in previous.rows if r.key not in new_keys]
        if not diff.empty():
            changes.tables.append(diff)
    return changes


def _short(value: str, limit: int = 16) -> str:
    value = value.replace("\n", " ").strip() or "空"
    return value if len(value) <= limit else value[: limit - 1] + "…"


def summary(changes: Changes, doc: Doc, limit: int = 120) -> str:
    """一句话的改动摘要，例如"明细新增 钢化玻璃；密封条 数量 8 → 10；备注：空 → 第二批"。明细的
    改动在前（通常最重要），字段在后。"""
    parts: list[str] = []
    tables = {t.key: t for t in doc.tables}
    for diff in changes.tables:
        table = tables[diff.key]
        labels = dict(table.columns)
        by_key = {r.key: r for r in table.rows}
        if diff.added:
            names = "、".join(_short(by_key[k].cells.get(table.title, "")) for k in diff.added)
            parts.append(f"{table.label}新增 {names}")
        if diff.removed:
            names = "、".join(_short(r.cells.get(table.title, "")) for r in diff.removed)
            parts.append(f"{table.label}删除 {names}")
        for row in diff.changed:
            name = _short(by_key[row.key].cells.get(table.title, ""))
            cells = "，".join(
                f"{labels.get(c.key, c.key)} {_short(c.before)} → {_short(c.after)}"
                for c in row.cells
            )
            parts.append(f"{name} {cells}")
    parts += [f"{c.label}：{_short(c.before)} → {_short(c.after)}" for c in changes.fields]
    text = "；".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"
