"""add talk intro outro columns

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-06 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: str | Sequence[str] | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'talks',
        sa.Column(
            'include_intro',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )
    op.add_column(
        'talks',
        sa.Column(
            'include_outro',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )
    op.add_column(
        'talks',
        sa.Column('intro_source', sa.String(50), nullable=True),
    )
    op.add_column(
        'talks',
        sa.Column('outro_source', sa.String(50), nullable=True),
    )
    op.add_column(
        'talks',
        sa.Column('custom_intro_path', sa.Text(), nullable=True),
    )
    op.add_column(
        'talks',
        sa.Column('custom_outro_path', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('talks', 'custom_outro_path')
    op.drop_column('talks', 'custom_intro_path')
    op.drop_column('talks', 'outro_source')
    op.drop_column('talks', 'intro_source')
    op.drop_column('talks', 'include_outro')
    op.drop_column('talks', 'include_intro')
