"""add role to turn_files

Revision ID: f2857596d603
Revises: 11f67b831f1e
Create Date: 2026-04-15 11:48:44.619037

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f2857596d603'
down_revision: Union[str, None] = '11f67b831f1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 幂等：仅当列不存在时才添加
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_name='turn_files' AND column_name='role'"
    ))
    if not result.fetchone():
        op.add_column('turn_files', sa.Column('role', sa.String(20), nullable=False, server_default='input'))


def downgrade() -> None:
    op.drop_column('turn_files', 'role')
