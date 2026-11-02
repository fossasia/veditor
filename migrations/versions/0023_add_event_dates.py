"""add event start_date and end_date

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("events", sa.Column("end_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "end_date")
    op.drop_column("events", "start_date")
