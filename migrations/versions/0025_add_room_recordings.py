"""Add room_recordings table and talk foreign key.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "room_recordings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("room", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("recording_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=50),
            server_default=sa.text("'ready'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_room_recordings_event_id", "room_recordings", ["event_id"])
    op.add_column(
        "talks",
        sa.Column("room_recording_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_talks_room_recording_id",
        "talks",
        "room_recordings",
        ["room_recording_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_talks_room_recording_id", "talks", ["room_recording_id"])


def downgrade() -> None:
    op.drop_index("ix_talks_room_recording_id", table_name="talks")
    op.drop_constraint("fk_talks_room_recording_id", "talks", type_="foreignkey")
    op.drop_column("talks", "room_recording_id")
    op.drop_index("ix_room_recordings_event_id", table_name="room_recordings")
    op.drop_table("room_recordings")
