"""Phase D: confirmation impact and resolution channel.

Two columns on ``confirmations``:

* ``impact`` — how far the action reaches, in one word, stored rather than
  re-derived so the record says what the user was actually shown even if a
  tool's classification changes later.
* ``resolution_channel`` — how the decision arrived. "Who approved this, and
  through what?" is a forensic question whose answer must not depend on
  correlating timestamps, and it is what makes the destructive-never-by-voice
  rule enforceable rather than merely stated.

Existing rows get ``impact='write'`` and a NULL channel. Backfilling a channel
would be inventing evidence: those decisions were made before the column
existed and nothing recorded how.

Revision ID: a3f1d90e77b2
Revises: 6dfc55cb77e4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3f1d90e77b2"
down_revision = "6dfc55cb77e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "confirmations",
        sa.Column("impact", sa.String(length=16), nullable=False,
                  server_default="write"),
    )
    op.add_column(
        "confirmations",
        sa.Column("resolution_channel", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("confirmations", "resolution_channel")
    op.drop_column("confirmations", "impact")
