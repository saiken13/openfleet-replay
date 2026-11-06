"""robot api keys

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "robot_api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("robot_id", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_robot_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_robot_api_keys_key_hash")),
    )
    op.create_index("ix_robot_api_keys_robot_id", "robot_api_keys", ["robot_id"])


def downgrade() -> None:
    op.drop_index("ix_robot_api_keys_robot_id", table_name="robot_api_keys")
    op.drop_table("robot_api_keys")
