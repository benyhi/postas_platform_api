from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, JSON, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MercadoPagoConnection(Base):
    __tablename__ = "mercado_pago_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_mercado_pago_connections_tenant"),
        Index("ix_mercado_pago_connections_tenant_id", "tenant_id"),
        Index("ix_mercado_pago_connections_collector_id", "collector_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_key_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collector_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    live_mode: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    application_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    integration_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active", nullable=False)
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MercadoPagoOAuthState(Base):
    __tablename__ = "mercado_pago_oauth_states"
    __table_args__ = (
        UniqueConstraint("state_hash", name="uq_mercado_pago_oauth_states_hash"),
        Index("ix_mercado_pago_oauth_states_tenant_id", "tenant_id"),
        Index("ix_mercado_pago_oauth_states_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    credential_key_id: Mapped[str] = mapped_column(String(80), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
