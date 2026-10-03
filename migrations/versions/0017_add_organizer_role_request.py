"""Add organizer role request fields to users table.

Revision ID: 0017_add_organizer_role_request
Revises: 0016_add_system_settings
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "organizer_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("organizer_request_note", sa.String(500), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "organizer_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_users_organizer_requested",
        "users",
        ["organizer_requested"],
    )


def downgrade() -> None:
    op.drop_index("idx_users_organizer_requested", table_name="users")
    op.drop_column("users", "organizer_requested_at")
    op.drop_column("users", "organizer_request_note")
    op.drop_column("users", "organizer_requested")
