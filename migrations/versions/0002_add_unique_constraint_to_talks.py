"""add unique constraint to talks

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29 23:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Preserve duplicate rows and their related jobs/reviews while making the
    # legacy key unique. Check every generated title against the current group
    # because a pre-existing non-duplicate title may use the same suffix.
    op.execute(
        """
        DO $$
        DECLARE
            duplicate RECORD;
            candidate TEXT;
            suffix TEXT;
            attempt INTEGER;
        BEGIN
            FOR duplicate IN
                SELECT later.id, later.event_id, later.start, later.title
                FROM talks AS later
                WHERE EXISTS (
                    SELECT 1
                    FROM talks AS earlier
                    WHERE earlier.event_id = later.event_id
                      AND earlier.start = later.start
                      AND earlier.title = later.title
                      AND earlier.id < later.id
                )
                ORDER BY later.id
            LOOP
                attempt := 1;
                LOOP
                    suffix := ' [duplicate ' || duplicate.id::text;
                    IF attempt > 1 THEN
                        suffix := suffix || '-' || attempt::text;
                    END IF;
                    suffix := suffix || ']';
                    candidate := left(
                        duplicate.title,
                        greatest(1, 255 - length(suffix))
                    ) || suffix;

                    EXIT WHEN NOT EXISTS (
                        SELECT 1
                        FROM talks AS existing
                        WHERE existing.event_id = duplicate.event_id
                          AND existing.start = duplicate.start
                          AND existing.title = candidate
                    );
                    attempt := attempt + 1;
                END LOOP;

                UPDATE talks
                SET title = candidate
                WHERE id = duplicate.id;
            END LOOP;
        END $$;
        """
    )
    op.create_unique_constraint(
        "uq_talks_event_id_title_start",
        "talks",
        ["event_id", "title", "start"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_talks_event_id_title_start", "talks", type_="unique")
