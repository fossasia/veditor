"""Normalize legacy blank talk room assignments.

Revision ID: 0023
Revises: 0022
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE talks SET room = NULL "
        "WHERE room IS NOT NULL AND trim(room) = ''"
    )


def downgrade() -> None:
    # Blank legacy assignments cannot be reconstructed safely.
    pass
