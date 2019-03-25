"""adding superior contact for redirection of calls

Revision ID: 19e1b49b8ef9
Revises: 2798bc43117a
Create Date: 2019-03-25 03:10:12.015160

"""

# revision identifiers, used by Alembic.
revision = '19e1b49b8ef9'
down_revision = '2798bc43117a'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('contact', sa.Column('superior_id', sa.Integer(), nullable=True))


def downgrade():
    pass
