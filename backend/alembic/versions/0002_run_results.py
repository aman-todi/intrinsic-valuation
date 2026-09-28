"""Run result columns: valuation_result, s3_prefix, pipeline_meta (tickets 11/12).

Revision ID: 0002_run_results
Revises: 0001_init
Create Date: 2026-09-28

- ``valuation_result`` — the run's own ValuationResult (a copy of the shared cached_models row on a
  cache hit, or the private forked result for custom runs), so GET /result never needs S3.
- ``s3_prefix`` — where the run's model.xlsx / report.pdf live (shared ``models/...`` or ``runs/{id}/``).
- ``pipeline_meta`` — small internal bag the jobs pass between classify and build (early-stage flag,
  risk-free rate at classification, SOTP segments, extra data flags, model_type_override, ...).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_run_results"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("valuation_result", postgresql.JSONB(), nullable=True))
    op.add_column("runs", sa.Column("s3_prefix", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("pipeline_meta", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "pipeline_meta")
    op.drop_column("runs", "s3_prefix")
    op.drop_column("runs", "valuation_result")
