"""the digest is the only delivery (NEU-1470)

ADR-0021 retires the alert mail, Web Push and the per-beat store setting. In order:

1. Every `alert`-kind and `push`-channel notification row is deleted. Nothing is lost by it:
   every event that earned an alert also earned a `digest` row, which stays.
2. `ck_notification_kind` and `ck_notification_channel` are tightened to the one value each
   keeps, so a stray writer cannot bring the dead vocabulary back.
3. `app.push_subscription` is dropped.
4. `user_settings.alert_stores` is dropped, check constraint first.

The downgrade recreates the table, the column and the wider constraints with their original
definitions, but not the rows: the deleted notifications and every browser's subscription are
gone, and a browser has to register again.

Revision ID: a92a8e808b07
Revises: de5bd1b49fe2
Create Date: 2026-09-26 01:14:07.000000+00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'a92a8e808b07'
down_revision = 'de5bd1b49fe2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM app.notification WHERE kind = 'alert' OR channel = 'push'")
    op.drop_constraint('ck_notification_kind', 'notification', schema='app')
    op.create_check_constraint(
        'ck_notification_kind', 'notification', "kind IN ('digest')", schema='app'
    )
    op.drop_constraint('ck_notification_channel', 'notification', schema='app')
    op.create_check_constraint(
        'ck_notification_channel', 'notification', "channel IN ('email')", schema='app'
    )
    op.drop_index('ix_push_subscription_user_id', table_name='push_subscription', schema='app')
    op.drop_table('push_subscription', schema='app')
    op.drop_constraint('ck_user_settings_alert_stores', 'user_settings', schema='app')
    op.drop_column('user_settings', 'alert_stores', schema='app')


def downgrade() -> None:
    op.add_column(
        'user_settings',
        sa.Column(
            'alert_stores',
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{stream}'::text[]"),
            nullable=False,
        ),
        schema='app',
    )
    op.create_check_constraint(
        'ck_user_settings_alert_stores',
        'user_settings',
        "alert_stores <@ ARRAY['buy', 'rent', 'stream']::text[]",
        schema='app',
    )
    op.create_table(
        'push_subscription',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('p256dh', sa.Text(), nullable=False),
        sa.Column('auth', sa.Text(), nullable=False),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(['user_id'], ['app.user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint'),
        schema='app',
    )
    op.create_index(
        'ix_push_subscription_user_id', 'push_subscription', ['user_id'], unique=False, schema='app'
    )
    op.drop_constraint('ck_notification_channel', 'notification', schema='app')
    op.create_check_constraint(
        'ck_notification_channel', 'notification', "channel IN ('email', 'push')", schema='app'
    )
    op.drop_constraint('ck_notification_kind', 'notification', schema='app')
    op.create_check_constraint(
        'ck_notification_kind', 'notification', "kind IN ('alert', 'digest')", schema='app'
    )
