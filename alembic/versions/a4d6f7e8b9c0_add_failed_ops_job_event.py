"""allow FAILED operational job events

Revision ID: a4d6f7e8b9c0
Revises: f36f4bfe4a0d
"""

from typing import Sequence, Union

from alembic import op

revision: str = "a4d6f7e8b9c0"
down_revision: Union[str, Sequence[str], None] = "f36f4bfe4a0d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_ops_job_event_ops_job_event_type_allowed", "ops_job_event", type_="check")
    op.create_check_constraint(
        "ck_ops_job_event_ops_job_event_type_allowed",
        "ops_job_event",
        "event_type IN ('START','END','WARN','ERROR','RETRY','FAILED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ops_job_event_ops_job_event_type_allowed", "ops_job_event", type_="check")
    op.create_check_constraint(
        "ck_ops_job_event_ops_job_event_type_allowed",
        "ops_job_event",
        "event_type IN ('START','END','WARN','ERROR','RETRY')",
    )
