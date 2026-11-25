from datetime import datetime
from uuid import uuid4
from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


class InboundEvent(Base):
    __tablename__ = "inbound_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    dedup_key: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(128))
    source_payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("account_id", "dedup_key"),)


class LeadTimer(Base):
    __tablename__ = "lead_timers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    account_id: Mapped[str] = mapped_column(String(128))
    lead_id: Mapped[str] = mapped_column(String(128))
    rule_code: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_id: Mapped[str | None] = mapped_column(ForeignKey("inbound_events.id"), nullable=True)
    __table_args__ = (Index("ix_timers_due", "status", "due_at"),)


class GHLConversation(Base):
    """Minimal CRM metadata; message contents are stored separately."""

    __tablename__ = "ghl_conversations"
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    contact_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GHLMessage(Base):
    """One record per GHL message and account, independently of polling frequency."""

    __tablename__ = "ghl_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ghl_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    contact_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(24), nullable=True)
    message_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("account_id", "ghl_message_id"),
        Index("ix_ghl_messages_conversation", "account_id", "conversation_id", "sent_at"),
    )


class GHLSyncRun(Base):
    """A PII-free audit record for a manually triggered read-only sync."""

    __tablename__ = "ghl_sync_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24), default="running")
    conversations_seen: Mapped[int] = mapped_column(default=0)
    messages_seen: Mapped[int] = mapped_column(default=0)
    messages_inserted: Mapped[int] = mapped_column(default=0)
    messages_duplicate: Mapped[int] = mapped_column(default=0)
    messages_skipped: Mapped[int] = mapped_column(default=0)
    truncated_conversations: Mapped[int] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GHLExportCheckpoint(Base):
    """Last fully imported UTC time, per location and message stream."""

    __tablename__ = "ghl_export_checkpoints"
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    stream: Mapped[str] = mapped_column(String(24), primary_key=True)
    high_water_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

