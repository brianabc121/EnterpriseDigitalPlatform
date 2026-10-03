"""布局参数单元测试，不连接或迁移本机数据库。"""

import unittest
from types import SimpleNamespace
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import Column, MetaData, Table, Uuid, create_engine
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, Unprocessable
from app.modules.iam import diagram
from app.modules.iam.schemas import StaffCreate, StaffDiagramNodeCreate
from app.modules.iam.service import create_staff


class StaffDiagramTests(unittest.TestCase):
    def payload(self, **layout):
        return StaffCreate(
            username="diagram-user",
            display_name="新员工",
            password="test-password",
            role_codes=["agent"],
            **layout,
        )

    def test_directions_and_legacy(self):
        self.assertIsNone(self.payload().diagram_direction)
        for direction in ("left", "right", "down"):
            parent = uuid4()
            created = self.payload(diagram_parent_id=parent, diagram_direction=direction)
            self.assertEqual(created.diagram_parent_id, parent)
            self.assertEqual(created.diagram_direction, direction)
            self.assertEqual(created.role_codes, ["agent"])

    def test_invalid_direction_and_incomplete_branch(self):
        with self.assertRaises(ValidationError):
            self.payload(diagram_direction="up")
        with self.assertRaises(ValidationError):
            self.payload(diagram_parent_id=uuid4())

    def test_draft_conversion_cannot_override_saved_layout(self):
        node_id = uuid4()
        self.assertEqual(self.payload(diagram_node_id=node_id).diagram_node_id, node_id)
        with self.assertRaises(ValidationError):
            self.payload(diagram_node_id=node_id, diagram_direction="left")


class SourceIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_and_foreign_tenant_parent_rejected_before_creation(self):
        engine = create_engine("sqlite://")
        metadata = MetaData()
        table = Table(
            "staff", metadata, Column("id", Uuid, primary_key=True), Column("tenant_id", Uuid)
        )
        metadata.create_all(engine)
        tenant, foreign_tenant, foreign_parent = uuid4(), uuid4(), uuid4()
        try:
            with Session(engine) as db:
                db.execute(table.insert().values(id=foreign_parent, tenant_id=foreign_tenant))
                db.commit()

                async def scalar(statement):
                    return db.scalar(statement)

                session = SimpleNamespace(scalar=scalar)
                for parent in (uuid4(), foreign_parent):
                    payload = StaffCreate(
                        username="new-user",
                        display_name="新员工",
                        password="test-password",
                        role_codes=["agent"],
                        diagram_parent_id=parent,
                        diagram_direction="down",
                    )
                    with self.assertRaises(Unprocessable):
                        await create_staff(
                            session, SimpleNamespace(tenant_id=tenant), payload, ip=None
                        )
        finally:
            engine.dispose()


class DraftPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cards_persist_without_accounts_and_children_keep_identity(self):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE staff (id CHAR(32) PRIMARY KEY, tenant_id CHAR(32))")
            conn.exec_driver_sql(
                "CREATE TABLE staff_diagram_nodes (id CHAR(32) PRIMARY KEY, tenant_id CHAR(32), "
                "parent_id CHAR(32), direction TEXT, staff_id CHAR(32), "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.exec_driver_sql(
                "CREATE TABLE audit_logs (id CHAR(32) PRIMARY KEY, tenant_id CHAR(32), "
                "actor_type TEXT, actor_id CHAR(32), action TEXT, resource_type TEXT, "
                "resource_id TEXT, detail TEXT, ip TEXT, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        tenant, actor = uuid4(), uuid4()
        principal = SimpleNamespace(tenant_id=tenant, staff_id=actor)
        try:
            with Session(engine) as db:

                class LocalAsyncSession:
                    def add(self, instance):
                        db.add(instance)

                    async def scalar(self, statement):
                        return db.scalar(statement)

                    async def scalars(self, statement):
                        return db.scalars(statement)

                    async def execute(self, statement):
                        return db.execute(statement)

                    async def delete(self, instance):
                        db.delete(instance)

                    async def commit(self):
                        db.commit()

                    async def refresh(self, instance):
                        db.refresh(instance)

                session = LocalAsyncSession()
                root = await diagram.create_node(
                    session, principal, StaffDiagramNodeCreate(direction="left"), ip=None
                )
                child = await diagram.create_node(
                    session,
                    principal,
                    StaffDiagramNodeCreate(parent_id=root.id, direction="down"),
                    ip=None,
                )
                self.assertEqual(
                    db.connection().exec_driver_sql("SELECT count(*) FROM staff").scalar(), 0
                )
                db.expire_all()
                rows = await diagram.list_nodes(session, tenant)
                self.assertEqual(len(rows), 2)
                self.assertEqual(next(row for row in rows if row.id == child.id).parent_id, root.id)
                foreign = SimpleNamespace(tenant_id=uuid4(), staff_id=actor)
                with self.assertRaises(Unprocessable):
                    await diagram.create_node(
                        session,
                        foreign,
                        StaffDiagramNodeCreate(parent_id=root.id, direction="right"),
                        ip=None,
                    )
                self.assertEqual(await diagram.list_nodes(session, foreign.tenant_id), [])
                with self.assertRaises(NotFound):
                    await diagram.delete_card(session, foreign, child.id, ip=None)
                await diagram.delete_card(session, principal, child.id, ip=None)
                child = await diagram.create_node(
                    session,
                    principal,
                    StaffDiagramNodeCreate(parent_id=root.id, direction="down"),
                    ip=None,
                )
                await diagram.delete_card(session, principal, root.id, ip=None)
                remaining = await diagram.list_nodes(session, tenant)
                self.assertEqual(len(remaining), 1)
                self.assertIsNone(remaining[0].parent_id)
                root = await diagram.create_node(
                    session, principal, StaffDiagramNodeCreate(direction="left"), ip=None
                )
                pending = await diagram.pending_node(session, tenant, root.id)
                pending.staff_id = uuid4()
                db.commit()
                with self.assertRaises(Conflict):
                    await diagram.pending_node(session, tenant, root.id)
                rows = await diagram.list_nodes(session, tenant)
                self.assertIsNone(next(row for row in rows if row.id == child.id).parent_id)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
