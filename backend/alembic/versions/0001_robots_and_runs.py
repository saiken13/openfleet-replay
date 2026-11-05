"""robots and runs

Revision ID: 0001
Revises:
Create Date: 2026-11-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "robots",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'OFFLINE'"), nullable=False),
        sa.Column("software_version", sa.Text(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("battery_pct", sa.Float(), nullable=True),
        sa.Column("cpu_pct", sa.Float(), nullable=True),
        sa.Column("mem_pct", sa.Float(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ONLINE', 'OFFLINE', 'DEGRADED', 'ERROR')",
            name=op.f("ck_robots_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_robots")),
    )

    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("robot_id", sa.Text(), nullable=False),
        sa.Column("software_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'RUNNING'"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'SUCCESS', 'FAILED', 'ABORTED')",
            name=op.f("ck_runs_status"),
        ),
        sa.ForeignKeyConstraint(["robot_id"], ["robots.id"], name=op.f("fk_runs_robot_id_robots")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index("ix_runs_robot_id_started_at", "runs", ["robot_id", sa.text("started_at DESC")])

    # robots <-> runs is circular, so this FK is added after both tables exist.
    op.create_foreign_key(
        op.f("fk_robots_current_run_id_runs"),
        "robots",
        "runs",
        ["current_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_robots_current_run_id_runs"), "robots", type_="foreignkey")
    op.drop_index("ix_runs_robot_id_started_at", table_name="runs")
    op.drop_table("runs")
    op.drop_table("robots")
