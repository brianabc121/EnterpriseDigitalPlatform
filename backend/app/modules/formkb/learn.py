"""每次提交表单都判断一次要不要更新表单知识（设计文档 §25.18）。

学习记录在提交表单的同一个事务里写入（record.py）；实时消费进程每秒领取待判断的记录（跨租户，
SKIP LOCKED，租约 60 秒），逐条判断，结果写在学习记录上；失败的稍后重试，3 次后放弃。

判断是幂等的：叫法的证据按提交记（同一次提交里同一个说法和商品只记一次），用量和搭配每次都按
单据的现状重新统计。

- 叫法：最近 20 次里选同一个商品的不少于 2 次（2 次不同的提交）、占三分之二以上生效；另一个商品
  达到这个条件时换成它（自己的占比降到三分之一以下时也回到观察中）；固定的（手工或改过的）不自动
  换，标待确认。系统已经会了的（输入就是这个商品的
  代码、名称、俗称或型号，而且排在第一个）不学。
- 用量：仓管确认领料单后，按这张单关联订单里的成品重新统计（只加工一种商品的订单，见
  warehouse/usage.py）：没有配方的是估算；配方里没有、常补领的材料不少于 2 张订单生效；和配方
  不一致的标待确认（配方不自动改）。
- 搭配：一起出现不少于 3 次、占 40% 以上生效，降到 30% 以下回到观察中。
"""

import logging
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.ids import new_id
from app.modules.formkb import service as formkb_service
from app.modules.formkb import settings as formkb_settings
from app.modules.formkb.models import (
    Event,
    Form,
    FormKbEntry,
    FormKbLog,
    FormKbSignal,
    FormKbSubmission,
    Kind,
    Review,
    Source,
    Status,
    SubmissionStatus,
    Via,
)
from app.modules.formkb.settings import FormKbSettings
from app.modules.formkb.text import alias_key, label_of, quantity, sentence
from app.modules.orders import service as order_service
from app.modules.orders.models import Order, OrderItem, OrderStatus
from app.modules.products import suggest
from app.modules.products.models import Product
from app.modules.warehouse import documents, usage
from app.modules.warehouse.models import (
    DocumentKind,
    DocumentStatus,
    StockDocument,
    StockDocumentLine,
)
from app.observability.context import bind_tenant

logger = logging.getLogger(__name__)

LEASE = timedelta(seconds=60)
RETRY = timedelta(seconds=30)
ATTEMPTS = 3
BATCH = 20

# 叫法：最近几次、最少几次、占多少。
ALIAS_WINDOW = 20
ALIAS_MIN = 2
ALIAS_SHARE = 2 / 3
# 生效的叫法：另一个商品达到生效条件（换成它）、或者自己的占比降到三分之一以下时回到观察中。
ALIAS_FLOOR = 1 / 3
# 用量：常补领的材料至少几张订单；和配方不一致至少几张订单、差多少；固定的用量差多少提示。
USAGE_EXTRA_MIN = 2
USAGE_DEVIATION_MIN = 3
USAGE_DEVIATION = Decimal("0.05")
USAGE_CONFLICT = Decimal("0.10")
# 搭配：看最近多少天、最多几张；记下来、生效、回到观察中的门槛。
COMPANION_DAYS = 90
COMPANION_FORMS = 200
COMPANION_OBSERVE = 2
COMPANION_MIN = 3
COMPANION_SHARE = 0.4
COMPANION_DROP = 0.3
COMPANION_ANCHORS = 30

KIND_TEXT = {Kind.ALIAS: "叫法", Kind.USAGE: "用量", Kind.COMPANION: "搭配"}


async def claim(ctx: AppContext, *, limit: int, now: datetime) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """领取到期的学习记录（跨租户，平台连接），返回 (租户, 学习记录)。"""
    async with ctx.db.platform_sessionmaker() as session:
        due = (
            select(FormKbSubmission.id)
            .where(
                FormKbSubmission.status == SubmissionStatus.PENDING,
                FormKbSubmission.due_at <= now,
            )
            .order_by(FormKbSubmission.due_at, FormKbSubmission.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = await session.execute(
            update(FormKbSubmission)
            .where(FormKbSubmission.id.in_(due.scalar_subquery()))
            .values(due_at=now + LEASE, attempts=FormKbSubmission.attempts + 1)
            .returning(FormKbSubmission.tenant_id, FormKbSubmission.id, FormKbSubmission.created_at)
        )
        claimed = sorted(rows.all(), key=lambda row: (row[2], row[1]))
        await session.commit()
    return [(tenant_id, submission_id) for tenant_id, submission_id, _ in claimed]


async def run_due(ctx: AppContext, *, limit: int = BATCH, now: datetime | None = None) -> int:
    """判断所有到期的学习记录（按提交的先后，逐条），返回处理的数量。"""
    claimed = await claim(ctx, limit=limit, now=now or datetime.now(UTC))
    for tenant_id, submission_id in claimed:
        with bind_tenant(tenant_id):
            try:
                await process(ctx, tenant_id, submission_id)
            except Exception:
                logger.exception("form knowledge judgement failed for %s", submission_id)
    return len(claimed)


async def process(ctx: AppContext, tenant_id: uuid.UUID, submission_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    async with ctx.db.tenant_session(tenant_id) as session:
        submission = await session.get(FormKbSubmission, submission_id)
        if submission is None or submission.status != SubmissionStatus.PENDING:
            return
        try:
            value = await formkb_settings.load(session, tenant_id)
            results = await Judge(session, submission, value, now).run()
        except Exception as exc:
            await session.rollback()
            logger.exception("form knowledge judgement failed for %s", submission_id)
            await _failed(session, submission_id, exc, now)
            return
        submission.status = SubmissionStatus.DONE.value
        submission.result = results
        submission.processed_at = now
        submission.due_at = None
        submission.error = None
        await session.commit()


async def _failed(
    session: AsyncSession, submission_id: uuid.UUID, exc: Exception, now: datetime
) -> None:
    submission = await session.get(FormKbSubmission, submission_id, populate_existing=True)
    if submission is None:
        return
    submission.error = f"{type(exc).__name__}: {exc}"[:500]
    if submission.attempts >= ATTEMPTS:
        submission.status = SubmissionStatus.FAILED.value
        submission.due_at = None
    else:
        submission.due_at = now + RETRY * submission.attempts
    await session.commit()


def _id(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _ids(values: Iterable[Any]) -> list[uuid.UUID]:
    found = [_id(v) for v in values]
    return list(dict.fromkeys(v for v in found if v is not None))


def counts_companions(form: Form, event: Event) -> bool:
    """搭配：订单在下单、改单时统计；领料单和入库单在确认（生效）后统计。"""
    if form == Form.ORDER:
        return event in (Event.CREATED, Event.UPDATED)
    return event == Event.CONFIRMED


def understood(query: str, product: Product, rank: int | None) -> bool:
    """系统已经会了：输入就是这个商品的代码、名称、俗称、型号（或拼音）等，而且排在第一个。"""
    if rank not in (None, 0):
        return False
    scored = suggest.score_all(query, [product])[0]
    return scored.match != "similar" and scored.score >= suggest.STRONG


class Judge:
    """一次提交的判断：学到、加强、生效、换成新的、待确认，或者用到；结果写在学习记录上。"""

    def __init__(
        self,
        session: AsyncSession,
        submission: FormKbSubmission,
        settings: FormKbSettings,
        now: datetime,
    ) -> None:
        self.session = session
        self.submission = submission
        self.settings = settings
        self.now = now
        self.results: list[dict[str, Any]] = []
        self.products: dict[uuid.UUID, Product] = {}
        # 这次提交里用到的叫法（只记"用到"，不再另记"加强"）。
        self.hit: set[uuid.UUID] = set()

    async def run(self) -> list[dict[str, Any]]:
        payload = self.submission.payload or {}
        form = Form(self.submission.form)
        event = Event(self.submission.event)
        traces = [t for t in payload.get("traces") or [] if isinstance(t, dict)]
        products = _ids(payload.get("products") or [])
        if traces:
            await self.load(_ids(t.get("product_id") for t in traces))
            if self.settings.learn_aliases:
                await self.aliases(traces)
            await self.companion_hits(form, traces, products)
        order_id = _id(payload.get("order_id"))
        if (
            self.settings.learn_usage
            and form == Form.REQUISITION
            and event == Event.CONFIRMED
            and order_id is not None
        ):
            await self.usage(order_id)
        if self.settings.learn_companions and products and counts_companions(form, event):
            await self.companions(form, products)
        return self.results

    # ---- 共用 ----

    async def load(self, ids: Iterable[uuid.UUID]) -> None:
        missing = [i for i in ids if i not in self.products]
        if missing:
            for product in await self.session.scalars(
                select(Product).where(Product.id.in_(missing))
            ):
                self.products[product.id] = product

    def describe(self, entry: FormKbEntry) -> str:
        product = self.products[entry.product_id]
        related = self.products.get(entry.related_id) if entry.related_id else None
        recipe = entry.stats.get("recipe") if entry.kind == Kind.USAGE else None
        # 判断结果里另有次数，搭配的句子不再重复。
        return sentence(
            entry, product, related, recipe=Decimal(recipe) if recipe else None, counts=False
        )

    def note(self, entry: FormKbEntry, action: str, text: str, *, log: bool = True) -> None:
        """记下判断结果（学习记录），需要时也记到这条知识的变化记录里。"""
        if log:
            self.session.add(
                FormKbLog(
                    tenant_id=self.submission.tenant_id,
                    entry_id=entry.id,
                    submission_id=self.submission.id,
                    action=action,
                    note=text,
                    actor_type="system",
                )
            )
        self.results.append(
            {
                "entry_id": str(entry.id),
                "kind": entry.kind,
                "action": action,
                "text": f"{KIND_TEXT[Kind(entry.kind)]}：{self.describe(entry)}（{text}）",
            }
        )

    def create(self, **fields: Any) -> FormKbEntry:
        entry = FormKbEntry(
            id=new_id(),
            tenant_id=self.submission.tenant_id,
            status=Status.OBSERVING.value,
            source=Source.LEARNED.value,
            learned_at=self.now,
            **fields,
        )
        self.session.add(entry)
        return entry

    def qualify(self, entry: FormKbEntry, qualifies: bool, *, drop: bool, why: str) -> None:
        """学到的、没有固定的知识：达到条件就生效（关掉了自动生效时标待确认），掉到下限以下回到
        观察中。停用的不动。"""
        if entry.status == Status.DISABLED or entry.locked:
            return
        if qualifies:
            if entry.status == Status.ACTIVE:
                return
            if self.settings.auto_activate:
                entry.status = Status.ACTIVE.value
                entry.review = entry.review_note = None
                self.note(entry, "activated", f"{why}，开始生效")
            elif entry.review != Review.ACTIVATE:
                entry.review = Review.ACTIVATE.value
                entry.review_note = f"{why}，确认后生效"
                self.note(entry, "review", entry.review_note)
            return
        if entry.review == Review.ACTIVATE:
            entry.review = entry.review_note = None
        if drop and entry.status == Status.ACTIVE:
            entry.status = Status.OBSERVING.value
            self.note(entry, "deactivated", f"{why}，回到观察中")

    async def _entries(self, kind: Kind, **where: Any) -> list[FormKbEntry]:
        statement = select(FormKbEntry).where(FormKbEntry.kind == kind)
        for column, value in where.items():
            field = getattr(FormKbEntry, column)
            statement = statement.where(
                field.in_(value) if isinstance(value, list | set | tuple) else field == value
            )
        return list(await self.session.scalars(statement.with_for_update()))

    # ---- 叫法 ----

    async def aliases(self, traces: list[dict[str, Any]]) -> None:
        touched: dict[str, None] = {}
        for trace in traces:
            product = self.products.get(_id(trace.get("product_id")) or uuid.UUID(int=0))
            if product is None:
                continue
            via = trace.get("via") or "suggest"
            rank = trace.get("rank") if isinstance(trace.get("rank"), int) else None
            pairs: list[tuple[str, str, Via]] = []
            missed, query = trace.get("missed"), trace.get("query")
            if isinstance(missed, str) and (key := alias_key(missed)):
                how = Via.MAPPED if via == "map" else Via.MISSED
                # 客户的说法、之前没找到的输入：本来就能直接找到这个商品时不用学。
                if not understood(missed, product, None):
                    pairs.append((key, label_of(missed), how))
            if isinstance(query, str) and via == "suggest" and (key := alias_key(query)):
                if trace.get("match") == "learned" and rank in (None, 0):
                    pairs.append((key, label_of(query), Via.HIT))
                elif not understood(query, product, rank):
                    pairs.append((key, label_of(query), Via.TYPED))
            for key, label, how in pairs:
                if await self._signal(key, label, product.id, how):
                    touched[key] = None
                    if how == Via.HIT:
                        await self._hit_alias(key, product.id)
        for key in touched:
            await self._alias_stats(key)

    async def _signal(self, key: str, label: str, product_id: uuid.UUID, how: Via) -> bool:
        """记一次叫法的证据；这次提交已经记过同一个说法和商品时不再记。"""
        statement = (
            insert(FormKbSignal)
            .values(
                id=new_id(),
                tenant_id=self.submission.tenant_id,
                submission_id=self.submission.id,
                text=key,
                label=label,
                product_id=product_id,
                via=how.value,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    FormKbSignal.tenant_id,
                    FormKbSignal.submission_id,
                    FormKbSignal.text,
                    FormKbSignal.product_id,
                ]
            )
            .returning(FormKbSignal.id)
        )
        return (await self.session.scalar(statement)) is not None

    async def _hit_alias(self, key: str, product_id: uuid.UUID) -> None:
        for entry in await self._entries(Kind.ALIAS, text=key, product_id=product_id):
            if entry.status == Status.ACTIVE:
                entry.hits += 1
                entry.last_hit_at = self.now
                self.hit.add(entry.id)
                self.note(entry, "hit", "这次开单用到了", log=False)

    async def _alias_stats(self, key: str) -> None:
        rows = (
            await self.session.execute(
                select(FormKbSignal.product_id, FormKbSignal.label)
                .where(FormKbSignal.text == key)
                .order_by(FormKbSignal.id.desc())
                .limit(ALIAS_WINDOW)
            )
        ).all()
        counts: Counter[uuid.UUID] = Counter(product_id for product_id, _ in rows)
        labels: dict[uuid.UUID, str] = {}
        for product_id, label in rows:
            labels.setdefault(product_id, label)
        total = sum(counts.values())
        existing = {e.product_id: e for e in await self._entries(Kind.ALIAS, text=key)}
        await self.load(set(counts) | set(existing))
        leader = max(counts, key=lambda p: counts[p]) if counts else None
        qualified = {
            p for p, n in counts.items() if n >= ALIAS_MIN and total and n / total >= ALIAS_SHARE
        }
        for product_id in list(dict.fromkeys([*counts, *existing])):
            if product_id not in self.products:
                continue
            n = counts.get(product_id, 0)
            share = n / total if total else 0.0
            entry = existing.get(product_id)
            stronger = False
            if entry is None:
                if not n:
                    continue
                entry = self.create(
                    kind=Kind.ALIAS.value,
                    form=None,
                    text=key,
                    label=labels.get(product_id, key),
                    product_id=product_id,
                    evidence=n,
                    share=share,
                    stats={"window": total},
                )
                await self.session.flush()
                self.note(entry, "created", f"第一次：最近 {total} 次里 {n} 次选了它")
            else:
                stronger = (
                    n > entry.evidence
                    and entry.status != Status.DISABLED
                    and entry.id not in self.hit
                )
                entry.evidence, entry.share = n, share
                entry.stats = {**entry.stats, "window": total}
                if not entry.locked and product_id in labels:
                    entry.label = labels[product_id]
            entry.learned_at = self.now
            why = f"最近 {total} 次里 {n} 次选了它"
            noted = len(self.results)
            self._judge_alias(entry, key, product_id, why, total, counts, leader, qualified)
            # 多了一次依据：这次没有别的变化（生效、待确认……）时记为"加强"。
            if stronger and len(self.results) == noted:
                self.note(entry, "strengthened", f"又选了一次，{why}")

    def _judge_alias(
        self,
        entry: FormKbEntry,
        key: str,
        product_id: uuid.UUID,
        why: str,
        total: int,
        counts: Counter[uuid.UUID],
        leader: uuid.UUID | None,
        qualified: set[uuid.UUID],
    ) -> None:
        """固定的叫法：别的商品达到条件时标待确认；学到的：达到条件生效，比例太低回到观察中。"""
        share = counts.get(product_id, 0) / total if total else 0.0
        if entry.locked:
            rival = leader if leader is not None and leader != product_id else None
            if (
                rival is not None
                and counts[rival] >= ALIAS_MIN
                and counts[rival] / total >= ALIAS_SHARE
                and entry.review != Review.CONFLICT
                and entry.status != Status.DISABLED
                and entry.stats.get("kept") != str(rival)
            ):
                other = self.products.get(rival)
                entry.stats = {**entry.stats, "rival": str(rival)}
                entry.review = Review.CONFLICT.value
                entry.review_note = (
                    f"最近 {total} 次输入「{entry.label or key}」，"
                    f"{counts[rival]} 次选了 {other.name if other else '另一个商品'}"
                )
                self.note(entry, "review", entry.review_note)
            return
        self.qualify(
            entry,
            product_id in qualified,
            drop=share < ALIAS_FLOOR or bool(qualified - {product_id}),
            why=why,
        )

    # ---- 用量 ----

    async def usage(self, order_id: uuid.UUID) -> None:
        items = await order_service.load_items(self.session, order_id)
        ready = await order_service.ready_made_ids(self.session, items)
        made = {i.product_id for i in documents.made_items(items, ready) if i.product_id}
        if not made:
            return
        recipes = await documents.boms(self.session, made)
        found = await usage.samples(self.session, made)
        existing = {
            (e.product_id, e.related_id): e
            for e in await self._entries(Kind.USAGE, product_id=made)
        }
        for product_id in sorted(made):
            rows = found.get(product_id, [])
            orders = len(rows)
            recipe = {r.material_id: r.quantity for r in recipes.get(product_id, [])}
            values_by = usage.per_unit(rows)
            materials = {m for m, v in values_by.items() if usage.frequent(v, orders)}
            materials |= {m for (p, m) in existing if p == product_id and m is not None}
            await self.load({product_id, *materials})
            for material_id in sorted(materials, key=lambda m: self._name(m)):
                if material_id not in self.products:
                    continue
                await self._material(
                    product_id,
                    material_id,
                    existing.get((product_id, material_id)),
                    values_by.get(material_id, []),
                    orders,
                    recipe,
                )

    def _name(self, product_id: uuid.UUID) -> str:
        product = self.products.get(product_id)
        return product.name if product else ""

    async def _material(
        self,
        product_id: uuid.UUID,
        material_id: uuid.UUID,
        entry: FormKbEntry | None,
        values: list[Decimal],
        orders: int,
        recipe: dict[uuid.UUID, Decimal],
    ) -> None:
        unit = self.products[material_id].unit
        per = self.products[product_id].unit or "件"
        often = bool(values) and usage.frequent(values, orders)
        learned = usage.rounded(usage.median(values)) if values else None
        planned = recipe.get(material_id)
        why = f"最近 {orders} 张订单里 {len(values)} 张领过"
        if not often or learned is None:
            if entry is None:
                return
            entry.evidence = len(values)
            entry.stats = {**entry.stats, "orders": orders, "with": len(values)}
            if entry.review == Review.RECIPE:
                entry.review = entry.review_note = None
            if not entry.locked and entry.status == Status.ACTIVE:
                entry.status = Status.OBSERVING.value
                self.note(entry, "deactivated", f"{why}，领得少了，回到观察中")
            return
        if planned is not None:
            deviating = (
                len(values) >= USAGE_DEVIATION_MIN
                and (all(v > planned for v in values) or all(v < planned for v in values))
                and abs(learned - planned) / planned >= USAGE_DEVIATION
            )
            basis = "deviation" if deviating else "recipe"
        else:
            basis = "extra" if recipe else "estimate"
        stats: dict[str, Any] = {
            **(entry.stats if entry is not None else {}),
            "basis": basis,
            "orders": orders,
            "with": len(values),
            "median": str(learned),
            "recipe": str(planned) if planned is not None else None,
        }
        if basis == "recipe":
            # 和配方一致：不需要单独的知识；以前学到的（例如后来加进了配方）回到观察中。
            if entry is None:
                return
            entry.stats, entry.evidence = stats, len(values)
            if entry.review == Review.RECIPE:
                entry.review = entry.review_note = None
                self.note(entry, "updated", f"{why}，和配方一致了")
            if not entry.locked and entry.status == Status.ACTIVE:
                entry.status = Status.OBSERVING.value
                self.note(entry, "deactivated", "已经在配方里，回到观察中")
            return
        if entry is None:
            entry = self.create(
                kind=Kind.USAGE.value,
                form=Form.REQUISITION.value,
                product_id=product_id,
                related_id=material_id,
                value=learned,
                evidence=len(values),
                stats=stats,
            )
            await self.session.flush()
            self.note(entry, "created", f"{why}，每{per}约 {quantity(learned, unit)}")
        else:
            entry.stats, entry.evidence = stats, len(values)
            entry.learned_at = self.now
            if not entry.locked and entry.value != learned:
                before = entry.value
                entry.value = learned
                if entry.status == Status.ACTIVE or basis == "deviation":
                    old = quantity(before, unit) if before is not None else "—"
                    self.note(
                        entry, "updated", f"{why}，每{per}从 {old} 改为 {quantity(learned, unit)}"
                    )
        if entry.locked:
            fixed = entry.value
            if (
                fixed
                and len(values) >= USAGE_DEVIATION_MIN
                and abs(learned - fixed) / fixed >= USAGE_CONFLICT
                and entry.review != Review.CONFLICT
                and entry.status != Status.DISABLED
                and entry.stats.get("kept") != str(learned)
            ):
                entry.review = Review.CONFLICT.value
                entry.review_note = (
                    f"{why}，每{per}实际约 {quantity(learned, unit)}，"
                    f"知识库里是 {quantity(fixed, unit)}"
                )
                self.note(entry, "review", entry.review_note)
            return
        if basis == "deviation":
            # 配方是主数据，不自动改：标待确认，由维护商品库的员工更新配方或保持不变（停用）；
            # 确认之前一键领料仍按配方。
            assert planned is not None
            if entry.status == Status.DISABLED:
                return
            if entry.status == Status.ACTIVE:
                entry.status = Status.OBSERVING.value
            if entry.review != Review.RECIPE:
                entry.review = Review.RECIPE.value
                entry.review_note = (
                    f"最近 {len(values)} 张订单每{per}实际约 {quantity(learned, unit)}，"
                    f"配方是 {quantity(planned, unit)}"
                )
                self.note(entry, "review", entry.review_note)
            return
        if entry.review == Review.RECIPE:
            entry.review = entry.review_note = None
        qualifies = basis == "estimate" or len(values) >= USAGE_EXTRA_MIN
        self.qualify(entry, qualifies, drop=False, why=why)

    # ---- 搭配 ----

    async def companions(self, form: Form, anchors: list[uuid.UUID]) -> None:
        since = self.now - timedelta(days=COMPANION_DAYS)
        for product_id in anchors[:COMPANION_ANCHORS]:
            ids = await self._forms_with(form, product_id, since)
            if not ids:
                continue
            together = await self._together(form, ids, product_id)
            existing = {
                e.related_id: e
                for e in await self._entries(Kind.COMPANION, form=form.value, product_id=product_id)
            }
            candidates = {y for y, c in together.items() if c >= COMPANION_OBSERVE}
            candidates |= {y for y in existing if y is not None}
            await self.load({product_id, *candidates})
            total = len(ids)
            for related_id in sorted(
                candidates, key=lambda y: (-together.get(y, 0), self._name(y))
            ):
                if related_id not in self.products or product_id not in self.products:
                    continue
                count = together.get(related_id, 0)
                share = count / total
                stats = {"forms": total, "together": count}
                entry = existing.get(related_id)
                if entry is None:
                    entry = self.create(
                        kind=Kind.COMPANION.value,
                        form=form.value,
                        product_id=product_id,
                        related_id=related_id,
                        value=Decimal(str(round(share, 3))),
                        evidence=count,
                        share=share,
                        stats=stats,
                    )
                    await self.session.flush()
                    self.note(entry, "created", f"{total} 张里 {count} 张一起开")
                else:
                    entry.evidence, entry.share, entry.stats = count, share, stats
                    entry.learned_at = self.now
                    if not entry.locked:
                        entry.value = Decimal(str(round(share, 3)))
                self.qualify(
                    entry,
                    count >= COMPANION_MIN and share >= COMPANION_SHARE,
                    drop=share < COMPANION_DROP,
                    why=f"{total} 张里 {count} 张一起开",
                )

    async def _forms_with(
        self, form: Form, product_id: uuid.UUID, since: datetime
    ) -> list[uuid.UUID]:
        """最近含有这个商品的同类表单（订单：没有取消的；领料单、入库单：已确认的）。"""
        if form == Form.ORDER:
            statement = (
                select(Order.id)
                .join(OrderItem, OrderItem.order_id == Order.id)
                .where(
                    OrderItem.product_id == product_id,
                    Order.created_at >= since,
                    Order.status != OrderStatus.CANCELLED,
                )
                .group_by(Order.id, Order.created_at)
                .order_by(Order.created_at.desc())
                .limit(COMPANION_FORMS)
            )
        else:
            statement = (
                select(StockDocument.id)
                .join(StockDocumentLine, StockDocumentLine.document_id == StockDocument.id)
                .where(
                    StockDocumentLine.product_id == product_id,
                    StockDocument.kind == DocumentKind(form.value),
                    StockDocument.status == DocumentStatus.CONFIRMED,
                    StockDocument.submitted_at >= since,
                )
                .group_by(StockDocument.id, StockDocument.submitted_at)
                .order_by(StockDocument.submitted_at.desc())
                .limit(COMPANION_FORMS)
            )
        return list(await self.session.scalars(statement))

    async def _together(
        self, form: Form, ids: list[uuid.UUID], product_id: uuid.UUID
    ) -> dict[uuid.UUID, int]:
        if form == Form.ORDER:
            statement = (
                select(OrderItem.product_id, func.count(func.distinct(OrderItem.order_id)))
                .where(
                    OrderItem.order_id.in_(ids),
                    OrderItem.product_id.is_not(None),
                    OrderItem.product_id != product_id,
                )
                .group_by(OrderItem.product_id)
            )
        else:
            statement = (
                select(
                    StockDocumentLine.product_id,
                    func.count(func.distinct(StockDocumentLine.document_id)),
                )
                .where(
                    StockDocumentLine.document_id.in_(ids),
                    StockDocumentLine.product_id != product_id,
                )
                .group_by(StockDocumentLine.product_id)
            )
        return {pid: int(n) for pid, n in (await self.session.execute(statement)).all() if pid}

    async def companion_hits(
        self, form: Form, traces: list[dict[str, Any]], products: list[uuid.UUID]
    ) -> None:
        """采用了"常一起开的"推荐：给推荐它的搭配记一次用到。"""
        picked = _ids(t.get("product_id") for t in traces if t.get("match") == "companion")
        if not picked:
            return
        entries = await self._entries(Kind.COMPANION, form=form.value, related_id=picked)
        hits: dict[uuid.UUID, list[FormKbEntry]] = defaultdict(list)
        for entry in entries:
            if entry.status == Status.ACTIVE and entry.product_id in products:
                hits[entry.related_id or uuid.UUID(int=0)].append(entry)
        await self.load({e.product_id for found in hits.values() for e in found} | set(picked))
        for found in hits.values():
            for entry in found:
                entry.hits += 1
                entry.last_hit_at = self.now
                self.note(entry, "hit", "这次开单用到了", log=False)


async def purge(ctx: AppContext) -> int:
    """调度进程：清理 90 天以前"没有需要更新的"学习记录（跨租户）。"""
    async with ctx.db.platform_sessionmaker() as session:
        removed = await formkb_service.purge_quiet(session, datetime.now(UTC))
        await session.commit()
    return removed
