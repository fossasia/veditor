"""add_speaker_role_and_email

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-22 19:50:59.598426

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('talks', sa.Column('speaker_email', sa.String(length=255), nullable=True))
    op.drop_constraint('ck_users_role', 'users', type_='check')
    op.create_check_constraint('ck_users_role', 'users', "role IN ('user', 'organizer', 'admin', 'speaker')")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_users_role', 'users', type_='check')
    op.execute("UPDATE users SET role = 'user' WHERE role = 'speaker'")
    op.create_check_constraint('ck_users_role', 'users', "role IN ('user', 'organizer', 'admin')")
    op.drop_column('talks', 'speaker_email')
