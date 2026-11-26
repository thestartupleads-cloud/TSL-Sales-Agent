"""M1 read-only lead states and response deadlines derived from stored messages.

This module does not call the GHL API, enqueue actions, or send messages. It
intentionally distinguishes confirmed user-attributed outbound replies from
automation and ambiguous outbound messages. Uncertain data requires review.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .models import GHLConversation, GHLMessage, LeadState


# GHL export includes non-conversational messages (calls, campaigns, etc.).
# Only these channels are currently eligible for a reply deadline.
REPLY_CHANNELS = (
    "SMS", "TYPE_SMS", "EMAIL", "TYPE_EMAIL", "WHATSAPP", "TYPE_WHATSAPP",
    "FACEBOOK", "TYPE_FACEBOOK", "INSTAGRAM", "TYPE_INSTAGRAM",
    "WEBCHAT", "TYPE_WEBCHAT", "LIVE_CHAT", "TYPE_LIVE_CHAT",
    "GMB", "TYPE_GMB",
)
# GHL call records are activity, not proof that a rep answered a chat.
# Keep the set conservative until we have verified actual export messageTypes.
CALL_CHANNELS = ("CALL", "TYPE_CALL")
AUTOMATED_SOURCES = ("workflow", "bulk_actions", "campaign", "api")
STATES = ("needs_reply", "waiting_customer", "needs_review", "outbound_only")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)  # SQLite strips offsets.
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    utc = _utc(value)
    return utc.isoformat().replace("+00:00", "Z") if utc else None


def _latest(session: Session, account_id: str, conversation_id: str, *filters):
    return session.scalar(
        select(GHLMessage)
        .where(
            GHLMessage.account_id == account_id,
            GHLMessage.conversation_id == conversation_id,
            GHLMessage.sent_at.is_not(None),
            *filters,
        )
        .order_by(GHLMessage.sent_at.desc(), GHLMessage.ghl_message_id.desc())
        .limit(1)
    )


def refresh_conversation_state(
    session: Session,
    account_id: str,
    conversation_id: str,
    *,
    sla_minutes: int = 30,
    now: datetime | None = None,
) -> LeadState:
    """Upsert exactly one state using message timestamps, not arrival order."""
    if not 1 <= sla_minutes <= 1440:
        raise ValueError("Response SLA must be between 1 and 1440 minutes")
    now = _utc(now or datetime.now(timezone.utc))

    last_message = _latest(session, account_id, conversation_id)
    channel = func.upper(GHLMessage.message_type)
    eligible = channel.in_(REPLY_CHANNELS)
    call_channel = channel.in_(CALL_CHANNELS)
    latest_chat = _latest(session, account_id, conversation_id, eligible)
    last_call = _latest(session, account_id, conversation_id, call_channel)
    last_inbound = _latest(session, account_id, conversation_id, eligible, GHLMessage.direction == "inbound")
    last_outbound = _latest(session, account_id, conversation_id, eligible, GHLMessage.direction == "outbound")

    # An outbound workflow/campaign/API event is not proof that a rep answered.
    non_automated = or_(
        GHLMessage.source.is_(None),
        func.lower(GHLMessage.source).notin_(AUTOMATED_SOURCES),
    )
    human_reply = _latest(
        session, account_id, conversation_id, eligible,
        GHLMessage.direction == "outbound", non_automated,
        GHLMessage.user_id.is_not(None), GHLMessage.user_id != "",
    )
    unverified_reply = _latest(
        session, account_id, conversation_id, eligible,
        GHLMessage.direction == "outbound", non_automated,
        or_(GHLMessage.user_id.is_(None), GHLMessage.user_id == ""),
    )

    untrustworthy = session.scalar(
        select(GHLMessage.id)
        .where(
            GHLMessage.account_id == account_id,
            GHLMessage.conversation_id == conversation_id,
            and_(
                # A call activity record may not have a message direction or
                # timestamp; it must not invalidate otherwise sound chat data.
                or_(GHLMessage.message_type.is_(None), ~call_channel),
                or_(
                    GHLMessage.sent_at.is_(None),
                    GHLMessage.direction.is_(None),
                    GHLMessage.direction.notin_(("inbound", "outbound")),
                ),
            ),
        )
        .limit(1)
    ) is not None

    inbound_at = _utc(last_inbound.sent_at) if last_inbound else None
    human_at = _utc(human_reply.sent_at) if human_reply else None
    unverified_at = _utc(unverified_reply.sent_at) if unverified_reply else None
    due_at = None
    reason = None

    if untrustworthy:
        state, reason = "needs_review", "missing_timestamp_or_direction"
    elif last_message is None:
        state, reason = "needs_review", "no_dated_messages"
    elif (
        (last_message.message_type or "").upper() not in REPLY_CHANNELS + CALL_CHANNELS
        and not (
            last_message.direction == "outbound"
            and (
                (last_message.source or "").lower() in AUTOMATED_SOURCES
                or "CAMPAIGN" in (last_message.message_type or "").upper()
            )
        )
    ):
        state, reason = "needs_review", "latest_channel_not_supported"
    elif latest_chat is None:
        # Calls alone cannot establish either a pending customer message or
        # a completed human reply.
        state, reason = "needs_review", "no_supported_chat_messages"
    elif inbound_at is None:
        state = "outbound_only"
    elif inbound_at > now + timedelta(minutes=5):
        state, reason = "needs_review", "future_message_timestamp"
    elif human_at is not None and human_at > inbound_at:
        state = "waiting_customer"
    elif human_at is not None and human_at == inbound_at:
        state, reason = "needs_review", "same_timestamp_reply"
    elif unverified_at is not None and unverified_at >= inbound_at:
        state, reason = "needs_review", "unverified_outbound_reply"
    elif last_call is not None and _utc(last_call.sent_at) > inbound_at:
        # A rep may have answered by phone. Until call direction and outcome
        # are verified, do not silently mark the lead answered or overdue.
        state, reason = "needs_review", "call_after_inbound_unverified"
    else:
        state = "needs_reply"
        due_at = inbound_at + timedelta(minutes=sla_minutes)

    # Complete all ORM lookups before adding a required-field row: subsequent
    # queries trigger autoflush and must never insert a half-populated state.
    conversation = session.get(GHLConversation, (account_id, conversation_id))
    row = session.get(LeadState, (account_id, conversation_id))
    if row is None:
        row = LeadState(account_id=account_id, conversation_id=conversation_id)
        session.add(row)

    row.contact_id = (conversation.contact_id if conversation else None) or (
        last_message.contact_id if last_message else None
    )
    row.state = state
    row.review_reason = reason
    row.last_message_at = _utc(last_message.sent_at) if last_message else None
    row.last_inbound_at = inbound_at
    row.last_outbound_at = _utc(last_outbound.sent_at) if last_outbound else None
    row.last_human_reply_at = human_at
    row.last_inbound_message_id = last_inbound.ghl_message_id if last_inbound else None
    row.response_due_at = due_at
    row.response_sla_minutes = sla_minutes
    session.flush()
    return row


def rebuild_lead_states(
    session: Session,
    account_id: str,
    *,
    limit: int = 100,
    after: str | None = None,
    force: bool = False,
    sla_minutes: int = 30,
    now: datetime | None = None,
) -> dict:
    """Rebuild a bounded page from historical local conversations, no GHL calls."""
    if not 1 <= limit <= 200:
        raise ValueError("Invalid rebuild page size")
    query = (
        select(GHLConversation.conversation_id)
        .where(GHLConversation.account_id == account_id)
        .order_by(GHLConversation.conversation_id)
    )
    if after:
        query = query.where(GHLConversation.conversation_id > after)
    if not force:
        query = query.outerjoin(
            LeadState,
            (LeadState.account_id == GHLConversation.account_id)
            & (LeadState.conversation_id == GHLConversation.conversation_id),
        ).where(LeadState.conversation_id.is_(None))
    ids = session.scalars(query.limit(limit + 1)).all()
    selected = ids[:limit]
    for conversation_id in selected:
        refresh_conversation_state(session, account_id, conversation_id, sla_minutes=sla_minutes, now=now)
    session.commit()
    has_more = len(ids) > limit
    return {"processed": len(selected), "has_more": has_more, "next_after": selected[-1] if has_more else None}


def tracker_summary(session: Session, account_id: str, *, active_days: int = 14, now: datetime | None = None) -> dict:
    now = _utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=active_days)
    criteria = (LeadState.account_id == account_id, LeadState.last_message_at >= cutoff)
    states = dict(session.execute(
        select(LeadState.state, func.count()).where(*criteria).group_by(LeadState.state)
    ).all())
    overdue = session.scalar(
        select(func.count()).select_from(LeadState).where(
            *criteria,
            LeadState.state == "needs_reply",
            LeadState.response_due_at <= now,
        )
    ) or 0
    return {
        "active_days": active_days,
        "conversations_tracked": sum(states.values()),
        "by_state": {name: states.get(name, 0) for name in STATES},
        "response_overdue": overdue,
        "customer_messages_sent": 0,
    }


def list_tracked_leads(
    session: Session,
    account_id: str,
    *,
    state: str | None = None,
    overdue_only: bool = False,
    active_days: int = 14,
    limit: int = 50,
    now: datetime | None = None,
) -> list[dict]:
    now = _utc(now or datetime.now(timezone.utc))
    query = select(LeadState).where(
        LeadState.account_id == account_id,
        LeadState.last_message_at >= now - timedelta(days=active_days),
    )
    if state is not None:
        if state not in STATES:
            raise ValueError("Unsupported lead state")
        query = query.where(LeadState.state == state)
    if overdue_only:
        query = query.where(LeadState.state == "needs_reply", LeadState.response_due_at <= now)
    rows = session.scalars(
        query.order_by(LeadState.last_message_at.desc(), LeadState.conversation_id).limit(limit)
    ).all()
    result = []
    for row in rows:
        latest = _latest(session, account_id, row.conversation_id)
        call = _latest(
            session, account_id, row.conversation_id,
            func.upper(GHLMessage.message_type).in_(CALL_CHANNELS),
        )
        result.append({
            "conversation_id": row.conversation_id,
            "contact_id": row.contact_id,
            "state": row.state,
            "review_reason": row.review_reason,
            "last_message_at": _iso(row.last_message_at),
            "last_inbound_at": _iso(row.last_inbound_at),
            "last_human_reply_at": _iso(row.last_human_reply_at),
            "response_due_at": _iso(row.response_due_at),
            "overdue": bool(
                row.state == "needs_reply"
                and row.response_due_at
                and _utc(row.response_due_at) <= now
            ),
            # Message metadata only. No message bodies, transcripts or phone numbers.
            "last_message_type": latest.message_type if latest else None,
            "last_message_direction": latest.direction if latest else None,
            "last_message_source": latest.source if latest else None,
            "last_call_at": _iso(call.sent_at) if call else None,
        })
    return result
