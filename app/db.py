"""Database models and session handling (SQLite locally, any SQLAlchemy URL in production)."""

from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .categories import CATEGORY_KEYS
from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_token() -> str:
    return secrets.token_urlsafe(24)


class Base(DeclarativeBase):
    pass


class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    cadence: Mapped[str] = mapped_column(String(10), default="daily")  # daily | weekly
    categories: Mapped[list] = mapped_column(JSON, default=lambda: list(CATEGORY_KEYS))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, default=new_token)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Edition(Base):
    __tablename__ = "editions"
    __table_args__ = (UniqueConstraint("cadence", "issue_date", name="uq_edition_cadence_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cadence: Mapped[str] = mapped_column(String(10))
    issue_date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD in the publication timezone
    issue_number: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="drafting")  # drafting|ready|sent|failed
    digest_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def digest(self) -> dict | None:
        return json.loads(self.digest_json) if self.digest_json else None


class CoveredStory(Base):
    """URLs and headlines that have already run, so later issues don't repeat them."""

    __tablename__ = "covered_stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), index=True)
    headline: Mapped[str] = mapped_column(String(500))
    edition_id: Mapped[int] = mapped_column(ForeignKey("editions.id"))
    covered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("edition_id", "subscriber_id", name="uq_delivery"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("editions.id"))
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"))
    provider_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="sent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
