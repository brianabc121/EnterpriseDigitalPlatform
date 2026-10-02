"""盈利报表导出（设计文档 §30.4）：Excel，工作表依次是利润表、每月、按商品、按客户、按处理人、
订单明细、收支明细。金额写成数字（元），比例写成百分数。"""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.xlsx import Cell, Column, Sheet, write_workbook
from app.modules.profit import entries as entry_service
from app.modules.profit import report
from app.modules.profit.periods import Period
from app.modules.profit.schemas import (
    BreakdownRow,
    CategoryAmount,
    Dimension,
    ProfitSummary,
    Statement,
)

MAX_ROWS = 5000


def _num(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _range(statement: Statement) -> str:
    return f"{statement.period.start:%Y-%m-%d} 至 {statement.period.end:%Y-%m-%d}"


def _categories(statements: list[Statement], kind: str) -> list[str]:
    names: list[str] = []
    for statement in statements:
        rows: list[CategoryAmount] = (
            statement.income_by_category if kind == "income" else statement.expense_by_category
        )
        names.extend(row.category for row in rows)
    return list(dict.fromkeys(names))


def _amount(statement: Statement, kind: str, category: str) -> float:
    rows = statement.income_by_category if kind == "income" else statement.expense_by_category
    return sum((float(r.amount) for r in rows if r.category == category), 0.0)


def _statement_sheet(
    summary: ProfitSummary, company: str, generated_by: str, now: datetime
) -> Sheet:
    cols = [summary.current, summary.previous, summary.last_year]
    rows: list[list[Cell]] = [
        ["企业", company, "", ""],
        ["期间", *[_range(s) for s in cols]],
        ["生成", f"{generated_by} {now:%Y-%m-%d %H:%M}", "", ""],
        ["", "", "", ""],
        ["一、销售收入", *[_num(s.revenue) for s in cols]],
        ["　订单数", *[s.orders for s in cols]],
        ["减：销售成本", *[_num(s.cost) for s in cols]],
        ["二、毛利", *[_num(s.gross_profit) for s in cols]],
        ["　毛利率（%）", *[s.gross_margin for s in cols]],
        ["加：其他收入", *[_num(s.other_income) for s in cols]],
    ]
    rows += [
        [f"　{name}", *[_amount(s, "income", name) for s in cols]]
        for name in _categories(cols, "income")
    ]
    rows.append(["减：费用", *[_num(s.expenses) for s in cols]])
    rows += [
        [f"　{name}", *[_amount(s, "expense", name) for s in cols]]
        for name in _categories(cols, "expense")
    ]
    rows += [
        ["三、净利润", *[_num(s.net_profit) for s in cols]],
        ["　净利率（%）", *[s.net_margin for s in cols]],
        ["", "", "", ""],
        ["回款（期间内登记的收款减退款）", _num(summary.collected), "", ""],
        ["本期订单未收", _num(summary.uncollected), "", ""],
        ["没有成本价的商品行（按 0 计算）", summary.cost_gap.lines, "", ""],
        ["　涉及的收入", _num(summary.cost_gap.revenue), "", ""],
    ]
    return Sheet(
        "利润表",
        [Column("项目", 30), Column("本期", 24), Column("上期", 24), Column("去年同期", 24)],
        rows=rows,
        validate_rows=0,
    )


def _money_columns() -> list[Column]:
    return [
        Column("销售收入", 14),
        Column("销售成本", 14),
        Column("毛利", 14),
        Column("毛利率（%）", 12),
        Column("占总毛利（%）", 12),
    ]


def _money_cells(row: BreakdownRow) -> list[Cell]:
    return [
        _num(row.revenue),
        _num(row.cost),
        _num(row.gross_profit),
        row.gross_margin,
        row.share,
    ]


async def workbook(
    session: AsyncSession,
    tz: ZoneInfo,
    period: Period,
    shift: int | None,
    *,
    company: str,
    generated_by: str,
    now: datetime,
    today: date,
) -> bytes:
    summary = await report.summary(session, tz, period, shift)
    trend = await report.trend(session, tz, period.end)

    async def rows(by: Dimension) -> list[BreakdownRow]:
        found = await report.breakdown(session, tz, period, by, limit=MAX_ROWS)
        return found.items

    products, customers, assignees, orders = (
        await rows("product"),
        await rows("customer"),
        await rows("assignee"),
        await rows("order"),
    )
    orders.sort(key=lambda r: (r.confirmed_on or date.min, r.label))
    listed = await entry_service.list_entries(session, period, today, limit=MAX_ROWS)

    sheets = [
        _statement_sheet(summary, company, generated_by, now),
        Sheet(
            "每月",
            [
                Column("月份", 10),
                Column("订单数", 8),
                Column("销售收入", 14),
                Column("销售成本", 14),
                Column("毛利", 14),
                Column("毛利率（%）", 12),
                Column("其他收入", 12),
                Column("费用", 12),
                Column("净利润", 14),
            ],
            rows=[
                [
                    m.month,
                    m.orders,
                    _num(m.revenue),
                    _num(m.cost),
                    _num(m.gross_profit),
                    m.gross_margin,
                    _num(m.other_income),
                    _num(m.expenses),
                    _num(m.net_profit),
                ]
                for m in trend.months
            ],
            validate_rows=0,
        ),
        Sheet(
            "按商品",
            [
                Column("商品", 24),
                Column("代码 · 型号 · 规格", 24),
                Column("订单数", 8),
                Column("销量", 8),
                *_money_columns(),
                Column("平均售价", 12),
                Column("单位成本", 12),
                Column("没有成本价的行", 14),
            ],
            rows=[
                [
                    r.label,
                    r.detail,
                    r.orders,
                    r.quantity,
                    *_money_cells(r),
                    _num(r.avg_price),
                    _num(r.unit_cost),
                    r.missing_cost,
                ]
                for r in products
            ],
            validate_rows=0,
        ),
        Sheet(
            "按客户",
            [
                Column("客户", 20),
                Column("公司", 20),
                Column("订单数", 8),
                *_money_columns(),
                Column("没有成本价的行", 14),
            ],
            rows=[
                [r.label, r.detail, r.orders, *_money_cells(r), r.missing_cost] for r in customers
            ],
            validate_rows=0,
        ),
        Sheet(
            "按处理人",
            [Column("处理人", 16), Column("订单数", 8), *_money_columns()],
            rows=[[r.label, r.orders, *_money_cells(r)] for r in assignees],
            validate_rows=0,
        ),
        Sheet(
            "订单明细",
            [
                Column("订单号", 20),
                Column("确认日期", 12),
                Column("客户", 20),
                Column("处理人", 12),
                Column("销售收入", 14),
                Column("销售成本", 14),
                Column("毛利", 14),
                Column("毛利率（%）", 12),
                Column("没有成本价的行", 14),
            ],
            rows=[
                [
                    r.label,
                    r.confirmed_on.isoformat() if r.confirmed_on else "",
                    r.detail,
                    r.assignee_name or "",
                    _num(r.revenue),
                    _num(r.cost),
                    _num(r.gross_profit),
                    r.gross_margin,
                    r.missing_cost,
                ]
                for r in orders
            ],
            validate_rows=0,
        ),
        Sheet(
            "收支明细",
            [
                Column("日期", 12),
                Column("类型", 8),
                Column("类别", 14),
                Column("金额", 14),
                Column("备注", 30),
                Column("每月固定", 10),
                Column("登记人", 12),
            ],
            rows=[
                [
                    e.occurred_on.isoformat(),
                    e.kind_label,
                    e.category,
                    _num(e.amount),
                    e.note,
                    "是" if e.recurring else "",
                    e.created_by_name or "",
                ]
                for e in sorted(listed.items, key=lambda e: (e.occurred_on, e.created_at))
            ],
            validate_rows=0,
        ),
    ]
    return write_workbook(sheets)
