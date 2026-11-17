"""add unique constraint to talks

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29 23:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: str | Sequence[str] | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint(
        'uq_talks_event_id_title_start',
        'talks',
        ['event_id', 'title', 'start'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_talks_event_id_title_start', 'talks', type_='unique')
