"""Mercado Pago OAuth and billing reservations

Revision ID: 20260907_01
Revises: 20260727_01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260907_01"
down_revision: str | None = "20260727_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_reservations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("feature_key", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Integer(), server_default="1", nullable=False),
        sa.Column("external_id", sa.String(length=120), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("period_key", sa.String(length=7), nullable=False),
        sa.Column("limit_value", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "feature_key", "idempotency_key", name="uq_billing_reservations_idempotency"
        ),
    )
    op.create_index("ix_billing_reservations_tenant_id", "billing_reservations", ["tenant_id"])
    op.create_index(
        "ix_billing_reservations_period",
        "billing_reservations",
        ["tenant_id", "feature_key", "period_key"],
    )
    op.create_index("ix_billing_reservations_status", "billing_reservations", ["status"])

    op.create_table(
        "mercado_pago_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("credential_key_id", sa.String(length=80), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collector_id", sa.String(length=80), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("live_mode", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("application_id", sa.String(length=80), nullable=True),
        sa.Column("integration_id", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("last_refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_mercado_pago_connections_tenant"),
    )
    op.create_index(
        "ix_mercado_pago_connections_tenant_id", "mercado_pago_connections", ["tenant_id"]
    )
    op.create_index(
        "ix_mercado_pago_connections_collector_id", "mercado_pago_connections", ["collector_id"]
    )

    op.create_table(
        "mercado_pago_oauth_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("code_verifier_encrypted", sa.Text(), nullable=False),
        sa.Column("credential_key_id", sa.String(length=80), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash", name="uq_mercado_pago_oauth_states_hash"),
    )
    op.create_index(
        "ix_mercado_pago_oauth_states_tenant_id", "mercado_pago_oauth_states", ["tenant_id"]
    )
    op.create_index(
        "ix_mercado_pago_oauth_states_expires_at", "mercado_pago_oauth_states", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_mercado_pago_oauth_states_expires_at", table_name="mercado_pago_oauth_states")
    op.drop_index("ix_mercado_pago_oauth_states_tenant_id", table_name="mercado_pago_oauth_states")
    op.drop_table("mercado_pago_oauth_states")
    op.drop_index("ix_mercado_pago_connections_collector_id", table_name="mercado_pago_connections")
    op.drop_index("ix_mercado_pago_connections_tenant_id", table_name="mercado_pago_connections")
    op.drop_table("mercado_pago_connections")
    op.drop_index("ix_billing_reservations_status", table_name="billing_reservations")
    op.drop_index("ix_billing_reservations_period", table_name="billing_reservations")
    op.drop_index("ix_billing_reservations_tenant_id", table_name="billing_reservations")
    op.drop_table("billing_reservations")
