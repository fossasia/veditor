"""Retain the requested calendar day for staged room recordings.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op


revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('room_recordings', sa.Column('target_date', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('room_recordings', 'target_date')
