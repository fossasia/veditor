"""Add organizer role request fields to users table.

Revision ID: 0019_add_organizer_role_request
Revises: 0018
Create Date: 2026-10-05
"""

import sqlalchemy as sa
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'organizer_requested',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )
    op.add_column(
        'users',
        sa.Column('organizer_request_note', sa.String(length=1000), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column(
            'organizer_requested_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'organizer_requested_at')
    op.drop_column('users', 'organizer_request_note')
    op.drop_column('users', 'organizer_requested')
