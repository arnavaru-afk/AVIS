"""expand corporate action types for security master

Revision ID: f36f4bfe4a0d
Revises: caa3037939ad
Create Date: 2026-06-28 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f36f4bfe4a0d"
down_revision: Union[str, Sequence[str], None] = "caa3037939ad"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_market_corporate_action_market_corp_action_type_allowed",
        "market_corporate_action",
        type_="check",
    )
    op.create_check_constraint(
        "ck_market_corporate_action_market_corp_action_type_allowed",
        "market_corporate_action",
        "action_type IN ('DIVIDEND','SPLIT','BONUS','RIGHTS','MERGER','DEMERGER','DELISTING')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_market_corporate_action_market_corp_action_type_allowed",
        "market_corporate_action",
        type_="check",
    )
    op.create_check_constraint(
        "ck_market_corporate_action_market_corp_action_type_allowed",
        "market_corporate_action",
        "action_type IN ('DIVIDEND','SPLIT','BONUS','RIGHTS','MERGER','DEMERGER')",
    )
