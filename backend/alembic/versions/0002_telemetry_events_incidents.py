"""telemetry, events, incidents

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_runs_started_at", "runs", [sa.text("started_at DESC")])
    op.create_table(
        "node_states",
        sa.Column("robot_id", sa.Text(), nullable=False),
        sa.Column("node", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("state_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('RUNNING', 'DEGRADED', 'STOPPED', 'ERROR')",
            name=op.f("ck_node_states_state"),
        ),
        sa.ForeignKeyConstraint(
            ["robot_id"], ["robots.id"], name=op.f("fk_node_states_robot_id_robots")
        ),
        sa.PrimaryKeyConstraint("robot_id", "node", name=op.f("pk_node_states")),
    )
    op.create_table(
        "topic_states",
        sa.Column("robot_id", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("expected_hz", sa.Float(), nullable=True),
        sa.Column("actual_hz", sa.Float(), nullable=False),
        sa.Column("last_msg_age_s", sa.Float(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('HEALTHY', 'DEGRADED', 'FAILED', 'UNKNOWN')",
            name=op.f("ck_topic_states_status"),
        ),
        sa.ForeignKeyConstraint(
            ["robot_id"], ["robots.id"], name=op.f("fk_topic_states_robot_id_robots")
        ),
        sa.PrimaryKeyConstraint("robot_id", "topic", name=op.f("pk_topic_states")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("robot_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'ERROR')", name=op.f("ck_events_severity")
        ),
        sa.CheckConstraint(
            "type IN ('run_started', 'run_ended', 'topic_status_changed', 'node_state_changed', 'log', 'robot_status_changed', 'software_version_changed', 'incident_opened', 'incident_escalated', 'incident_resolved')",
            name=op.f("ck_events_type"),
        ),
        sa.ForeignKeyConstraint(
            ["robot_id"], ["robots.id"], name=op.f("fk_events_robot_id_robots")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_events_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index(
        "ix_events_robot_id_received_at", "events", ["robot_id", "received_at"], unique=False
    )
    op.create_index("ix_events_robot_id_ts", "events", ["robot_id", "ts"], unique=False)
    op.create_index("ix_events_run_id_ts", "events", ["run_id", "ts"], unique=False)
    op.create_table(
        "incidents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("robot_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'OPEN'"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("software_version", sa.Text(), nullable=True),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'CRITICAL')", name=op.f("ck_incidents_severity")
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'INVESTIGATING', 'RESOLVED')", name=op.f("ck_incidents_status")
        ),
        sa.ForeignKeyConstraint(
            ["robot_id"], ["robots.id"], name=op.f("fk_incidents_robot_id_robots")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_incidents_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incidents")),
    )
    op.create_index(
        "ix_incidents_robot_id_started_at", "incidents", ["robot_id", "started_at"], unique=False
    )
    op.create_index("ix_incidents_run_id", "incidents", ["run_id"], unique=False)
    op.create_index("ix_incidents_started_at", "incidents", ["started_at"], unique=False)
    op.create_index(
        "uq_incidents_robot_id_dedup_key_unresolved",
        "incidents",
        ["robot_id", "dedup_key"],
        unique=True,
        postgresql_where=sa.text("status <> 'RESOLVED'"),
    )
    op.create_table(
        "telemetry_samples",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("robot_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("cpu_pct", sa.Float(), nullable=True),
        sa.Column("mem_pct", sa.Float(), nullable=True),
        sa.Column("battery_pct", sa.Float(), nullable=True),
        sa.Column("net_latency_ms", sa.Float(), nullable=True),
        sa.Column("pose_x", sa.Float(), nullable=True),
        sa.Column("pose_y", sa.Float(), nullable=True),
        sa.Column("pose_yaw", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["robot_id"], ["robots.id"], name=op.f("fk_telemetry_samples_robot_id_robots")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_telemetry_samples_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telemetry_samples")),
        sa.UniqueConstraint("run_id", "seq", name=op.f("uq_telemetry_samples_run_id_seq")),
    )
    op.create_index(
        "ix_telemetry_samples_robot_id_received_at",
        "telemetry_samples",
        ["robot_id", "received_at"],
        unique=False,
    )
    op.create_index(
        "ix_telemetry_samples_run_id_ts", "telemetry_samples", ["run_id", "ts"], unique=False
    )
    op.create_table(
        "topic_samples",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("robot_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("expected_hz", sa.Float(), nullable=True),
        sa.Column("actual_hz", sa.Float(), nullable=False),
        sa.Column("last_msg_age_s", sa.Float(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('HEALTHY', 'DEGRADED', 'FAILED', 'UNKNOWN')",
            name=op.f("ck_topic_samples_status"),
        ),
        sa.ForeignKeyConstraint(
            ["robot_id"], ["robots.id"], name=op.f("fk_topic_samples_robot_id_robots")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_topic_samples_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topic_samples")),
        sa.UniqueConstraint(
            "run_id", "seq", "topic", name=op.f("uq_topic_samples_run_id_seq_topic")
        ),
    )
    op.create_index(
        "ix_topic_samples_run_id_topic_ts", "topic_samples", ["run_id", "topic", "ts"], unique=False
    )
    op.create_index("ix_topic_samples_run_id_ts", "topic_samples", ["run_id", "ts"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_started_at", table_name="runs")
    op.drop_index("ix_topic_samples_run_id_ts", table_name="topic_samples")
    op.drop_index("ix_topic_samples_run_id_topic_ts", table_name="topic_samples")
    op.drop_table("topic_samples")
    op.drop_index("ix_telemetry_samples_run_id_ts", table_name="telemetry_samples")
    op.drop_index("ix_telemetry_samples_robot_id_received_at", table_name="telemetry_samples")
    op.drop_table("telemetry_samples")
    op.drop_index(
        "uq_incidents_robot_id_dedup_key_unresolved",
        table_name="incidents",
        postgresql_where=sa.text("status <> 'RESOLVED'"),
    )
    op.drop_index("ix_incidents_started_at", table_name="incidents")
    op.drop_index("ix_incidents_run_id", table_name="incidents")
    op.drop_index("ix_incidents_robot_id_started_at", table_name="incidents")
    op.drop_table("incidents")
    op.drop_index("ix_events_run_id_ts", table_name="events")
    op.drop_index("ix_events_robot_id_ts", table_name="events")
    op.drop_index("ix_events_robot_id_received_at", table_name="events")
    op.drop_table("events")
    op.drop_table("topic_states")
    op.drop_table("node_states")
