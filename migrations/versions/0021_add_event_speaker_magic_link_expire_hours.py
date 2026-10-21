"""add event speaker magic link expire hours

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column(
            "speaker_magic_link_expire_hours",
            sa.Integer(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("events", "speaker_magic_link_expire_hours")
