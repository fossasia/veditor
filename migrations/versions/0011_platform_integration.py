"""platform integration and external identifiers

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-14 01:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Update clients table
    op.add_column(
        "clients",
        sa.Column("name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column(
            "is_platform",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    # 2. Update events table
    op.add_column(
        "events",
        sa.Column("source", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column("external_id", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_events_source_external_id",
        "events",
        ["source", "external_id"],
    )

    # 3. Update talks table
    op.add_column(
        "talks",
        sa.Column("external_id", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_talks_event_id_external_id",
        "talks",
        ["event_id", "external_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_talks_event_id_external_id", "talks", type_="unique")
    op.drop_column("talks", "external_id")

    op.drop_constraint("uq_events_source_external_id", "events", type_="unique")
    op.drop_column("events", "external_id")
    op.drop_column("events", "source")

    op.drop_column("clients", "is_platform")
    op.drop_column("clients", "name")
