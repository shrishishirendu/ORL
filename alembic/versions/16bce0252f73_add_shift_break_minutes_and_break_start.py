"""add shift break_minutes and break_start

Revision ID: 16bce0252f73
Revises: 7c3e9a1d5b20
Create Date: 2026-09-29 06:07:38.176330

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '16bce0252f73'
down_revision: Union[str, Sequence[str], None] = '7c3e9a1d5b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Both nullable: NULL means "break not known", and the award engine is then
# sent 0 minutes, exactly as before this column existed (see Shift's docstring).
def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('shift', sa.Column('break_minutes', sa.Integer(), nullable=True))
    op.add_column('shift', sa.Column('break_start', sa.Time(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('shift', 'break_start')
    op.drop_column('shift', 'break_minutes')
