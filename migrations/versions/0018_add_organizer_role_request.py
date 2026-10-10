"""Add organizer role request fields to users table.

Revision ID: 0018_add_organizer_role_request
Revises: 0017_add_user_email_verification
Create Date: 2026-10-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
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
        sa.Column("organizer_request_note", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "organizer_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "organizer_requested_at")
    op.drop_column("users", "organizer_request_note")
    op.drop_column("users", "organizer_requested")
