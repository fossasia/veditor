"""add event timezone

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-29

"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("timezone", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "timezone")
