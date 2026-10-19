"""mockpay's own tables. Created with metadata.create_all at startup: this is a test double,
so it has no migration history to preserve."""

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, BigInteger, CheckConstraint, ForeignKey, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {int: BigInteger}


class MpPayment(Base):
    __tablename__ = "mp_payments"
    __table_args__ = (
        CheckConstraint("0 <= captured_paise AND captured_paise <= amount_paise"),
        # Like real providers, mockpay refuses to refund more than was captured in total.
        CheckConstraint("0 <= refunded_paise AND refunded_paise <= captured_paise"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    amount_paise: Mapped[int]
    captured_paise: Mapped[int]
    refunded_paise: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MpRefund(Base):
    __tablename__ = "mp_refunds"
    __table_args__ = (
        CheckConstraint("amount_paise > 0"),
        CheckConstraint("status IN ('pending', 'processed', 'failed')"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    payment_id: Mapped[str] = mapped_column(ForeignKey("mp_payments.id"), index=True)
    amount_paise: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), default="pending")
    receipt: Mapped[str | None] = mapped_column(String(128), index=True)
    notes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Fault behaviour fixed at creation, so later webhooks for this refund follow it.
    webhook_mode: Mapped[str] = mapped_column(String(32), default="")
    will_fail: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]


class MpIdempotencyKey(Base):
    __tablename__ = "mp_idempotency_keys"
    __table_args__ = (CheckConstraint("state IN ('in_flight', 'done')"),)

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    response_status: Mapped[int | None]
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MpEvent(Base):
    __tablename__ = "mp_events"

    sequence: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(32), unique=True)
    type: Mapped[str] = mapped_column(String(32))
    refund_id: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
