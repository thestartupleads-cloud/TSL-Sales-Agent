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

