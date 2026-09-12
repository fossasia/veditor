"""add client webhook columns

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-13 03:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("clients", sa.Column("webhook_url", sa.Text(), nullable=True))
    op.add_column(
        "clients", sa.Column("webhook_secret", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("clients", "webhook_secret")
    op.drop_column("clients", "webhook_url")
