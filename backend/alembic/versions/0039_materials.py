"""Enterprise materials on Alibaba Cloud OSS (design §36): folders, materials, share links

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("material_folders", "materials", "material_shares")

STATEMENTS = [
    # 多层级文件夹（最多 5 层）：同一上级下名称不重复；有下级或资料的文件夹不能删除。
    """
    CREATE TABLE material_folders (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      parent_id uuid,
      name varchar(64) NOT NULL,
      sort integer NOT NULL DEFAULT 0,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      CONSTRAINT uq_material_folders_name UNIQUE NULLS NOT DISTINCT (tenant_id, parent_id, name),
      FOREIGN KEY (tenant_id, parent_id) REFERENCES material_folders (tenant_id, id),
      CONSTRAINT ck_material_folders_parent CHECK (parent_id IS DISTINCT FROM id)
    )
    """,
    # 资料：文件在 OSS 上（object_key 是 {前缀}{企业 ID}/{资料 ID}{扩展名}），这里只记信息。
    # 上传中的记录 24 小时没有完成由调度任务清理；upload_id 是分片上传的 ID。
    # scan_status：pending（等待扫描）、clean、infected、skipped（超过扫描上限）、missing；
    # 视频、文字资料和没有配置病毒扫描时为空。excerpt 是文字资料正文的前 2,000 字。
    """
    CREATE TABLE materials (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      folder_id uuid,
      kind varchar(12) NOT NULL,
      name varchar(200) NOT NULL,
      description text,
      tags varchar(32)[] NOT NULL DEFAULT '{}',
      file_name varchar(255) NOT NULL,
      ext varchar(16) NOT NULL,
      content_type varchar(128) NOT NULL,
      size bigint NOT NULL,
      object_key text NOT NULL,
      upload_id varchar(128),
      part_size integer,
      status varchar(12) NOT NULL DEFAULT 'uploading',
      scan_status varchar(12),
      scan_signature varchar(200),
      scanned_at timestamptz,
      excerpt text,
      views integer NOT NULL DEFAULT 0,
      downloads integer NOT NULL DEFAULT 0,
      created_by uuid,
      updated_by uuid,
      uploaded_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      CONSTRAINT uq_materials_object_key UNIQUE (object_key),
      FOREIGN KEY (tenant_id, folder_id) REFERENCES material_folders (tenant_id, id),
      CONSTRAINT ck_materials_kind CHECK (kind IN ('video', 'document', 'image', 'text', 'other')),
      CONSTRAINT ck_materials_status CHECK (status IN ('uploading', 'ready', 'blocked')),
      CONSTRAINT ck_materials_scan CHECK (scan_status IS NULL
        OR scan_status IN ('pending', 'clean', 'infected', 'skipped', 'missing')),
      CONSTRAINT ck_materials_size CHECK (size >= 0)
    )
    """,
    "CREATE INDEX ix_materials_folder ON materials (tenant_id, folder_id)",
    "CREATE INDEX ix_materials_created ON materials (tenant_id, created_at DESC)",
    "CREATE INDEX ix_materials_tags ON materials USING gin (tags)",
    "CREATE INDEX ix_materials_uploading ON materials (created_at) WHERE status = 'uploading'",
    "CREATE INDEX ix_materials_scan ON materials (uploaded_at) WHERE scan_status = 'pending'",
    # 分享链接：客户凭令牌打开 Widget 上的分享页（1–30 天有效，可以随时停用）。
    """
    CREATE TABLE material_shares (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      material_id uuid NOT NULL,
      token varchar(64) NOT NULL,
      expires_at timestamptz NOT NULL,
      disabled_at timestamptz,
      disabled_by uuid,
      created_by uuid,
      opens integer NOT NULL DEFAULT 0,
      last_opened_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      CONSTRAINT uq_material_shares_token UNIQUE (token),
      FOREIGN KEY (tenant_id, material_id) REFERENCES materials (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_material_shares_material ON material_shares (tenant_id, material_id)",
]
# 默认套餐的企业资料存储额度（GB）；运营改过的（已经有这一项的）不动。
PLAN_STORAGE = {"trial": 1, "standard": 50, "enterprise": 500}


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    for table in TENANT_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edp_app")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edp_platform")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (tenant_id = app_current_tenant())
              WITH CHECK (tenant_id = app_current_tenant())
            """
        )
        op.execute(
            f"CREATE POLICY platform_access ON {table} TO edp_platform"
            " USING (true) WITH CHECK (true)"
        )
        # 增量更新索引（§33.9）。
        op.execute(f"SELECT edp_track_changes('{table}'::regclass)")
    for code, gb in PLAN_STORAGE.items():
        op.execute(
            "UPDATE plans SET limits = limits || jsonb_build_object('material_gb', "
            f"{gb}) WHERE code = '{code}' AND NOT limits ? 'material_gb'"
        )


def downgrade() -> None:
    op.execute("UPDATE plans SET limits = limits - 'material_gb'")
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE {table}")
