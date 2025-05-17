"""Support multiple webhook methods

Revision ID: ef08aba23d77
Revises: 0b9bf4cd7276
Create Date: 2025-05-16 12:38:37.586828

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ef08aba23d77"
down_revision: str | None = "0b9bf4cd7276"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add the methods column with a server default of ["POST"]
    op.add_column(
        "webhook",
        sa.Column(
            "methods",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default=sa.text("'[\"POST\"]'::jsonb"),
        ),
    )

    # Populate methods with an array containing the existing method for existing rows
    op.execute("""
        UPDATE webhook
        SET methods = jsonb_build_array(method)
        WHERE method IS NOT NULL
    """)

    # Now set NOT NULL constraint
    op.alter_column("webhook", "methods", nullable=False)

    # Finally drop the method column
    op.drop_column("webhook", "method")


def downgrade() -> None:
    # First add the method column allowing NULL
    op.add_column("webhook", sa.Column("method", sa.VARCHAR(), nullable=True))

    # Set all method values to POST
    op.execute("UPDATE webhook SET method = 'POST'")

    # Now set NOT NULL constraint
    op.alter_column("webhook", "method", nullable=False)

    # Finally drop the methods column
    op.drop_column("webhook", "methods")
