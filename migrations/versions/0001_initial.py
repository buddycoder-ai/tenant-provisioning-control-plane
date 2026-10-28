"""Initial tenant control-plane schema."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(28), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tasks_tenant_created", "tasks", ["tenant_id", "created_at"])
    op.create_index("uq_tasks_one_open_per_tenant", "tasks", ["tenant_id"], unique=True,
                    postgresql_where=sa.text("status IN ('accepted', 'in_progress')"))
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False, unique=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(500)),
    )
    op.create_table(
        "inbox_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("inbox_events")
    op.drop_table("outbox_events")
    op.drop_index("uq_tasks_one_open_per_tenant", table_name="tasks")
    op.drop_index("ix_tasks_tenant_created", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("tenants")
