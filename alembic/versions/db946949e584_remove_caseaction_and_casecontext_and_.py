"""Remove CaseAction and CaseContext and remove workspace ID from CaseEvent

Revision ID: db946949e584
Revises: 50e22ca490f9
Create Date: 2024-08-23 01:07:33.909731

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "db946949e584"
down_revision: str | None = "50e22ca490f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index("ix_caseaction_id", table_name="caseaction")
    op.drop_table("caseaction")
    op.drop_index("ix_casecontext_id", table_name="casecontext")
    op.drop_table("casecontext")
    op.alter_column("caseevent", "case_id", existing_type=sa.VARCHAR(), nullable=True)
    op.create_foreign_key(
        None, "caseevent", "case", ["case_id"], ["id"], ondelete="CASCADE"
    )
    op.drop_column("caseevent", "workflow_id")
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column(
        "caseevent",
        sa.Column("workflow_id", sa.VARCHAR(), autoincrement=False, nullable=False),
    )
    op.drop_constraint(None, "caseevent", type_="foreignkey")  # type: ignore
    op.alter_column("caseevent", "case_id", existing_type=sa.VARCHAR(), nullable=False)
    op.create_table(
        "casecontext",
        sa.Column("surrogate_id", sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("(now() AT TIME ZONE 'utc'::text)"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("(now() AT TIME ZONE 'utc'::text)"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("id", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column("tag", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column("value", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.UUID(), autoincrement=False, nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name="casecontext_user_id_fkey"
        ),
        sa.PrimaryKeyConstraint("surrogate_id", name="casecontext_pkey"),
    )
    op.create_index("ix_casecontext_id", "casecontext", ["id"], unique=True)
    op.create_table(
        "caseaction",
        sa.Column("surrogate_id", sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("(now() AT TIME ZONE 'utc'::text)"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("(now() AT TIME ZONE 'utc'::text)"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("id", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column("tag", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column("value", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.UUID(), autoincrement=False, nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name="caseaction_user_id_fkey"
        ),
        sa.PrimaryKeyConstraint("surrogate_id", name="caseaction_pkey"),
    )
    op.create_index("ix_caseaction_id", "caseaction", ["id"], unique=True)
    # ### end Alembic commands ###
