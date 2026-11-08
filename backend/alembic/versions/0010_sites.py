"""sites: multi-tenancy by site or team (DECISIONS #039)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-08 21:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sites",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sites"),
    )
    op.execute("INSERT INTO sites (id, name) VALUES ('default', 'Default site')")
    # Existing robots join the default site; existing users keep seeing everything.
    op.add_column(
        "robots",
        sa.Column("site_id", sa.Text(), server_default=sa.text("'default'"), nullable=False),
    )
    op.create_foreign_key("fk_robots_site_id_sites", "robots", "sites", ["site_id"], ["id"])
    op.create_index("ix_robots_site_id", "robots", ["site_id"])
    op.add_column("users", sa.Column("site_id", sa.Text(), nullable=True))
    op.create_foreign_key("fk_users_site_id_sites", "users", "sites", ["site_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_users_site_id_sites", "users", type_="foreignkey")
    op.drop_column("users", "site_id")
    op.drop_index("ix_robots_site_id", table_name="robots")
    op.drop_constraint("fk_robots_site_id_sites", "robots", type_="foreignkey")
    op.drop_column("robots", "site_id")
    op.drop_table("sites")
