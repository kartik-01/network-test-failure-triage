"""SQLite tables for cases, runs, and the insert-only audit log.

There is no update or delete operation for audit rows. The database also
rejects those statements with triggers installed by tracker.db.
"""

from __future__ import annotations

from datetime import datetime
from typing import Type

from sqlalchemy import DateTime, ForeignKey, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UtcDateTime(TypeDecorator[datetime]):
    """Store UTC timestamps and always return them timezone-aware."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        from datetime import timezone

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        from datetime import timezone

        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class CaseRow(Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    test_case: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text)
    component: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(8), nullable=True)
    rerun_recorded: Mapped[bool] = mapped_column(default=False)
    latest_build: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime)

    runs: Mapped[list[RunRow]] = relationship(back_populates="case")
    audits: Mapped[list[AuditRow]] = relationship(back_populates="case", order_by="AuditRow.id")


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    build: Mapped[str] = mapped_column(String(64), index=True)
    test_case: Mapped[str] = mapped_column(String(64), index=True)
    result: Mapped[str] = mapped_column(String(8))
    component: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    effect: Mapped[str] = mapped_column(String(32))
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), nullable=True, index=True)
    ingested_at: Mapped[datetime] = mapped_column(UtcDateTime)

    case: Mapped[CaseRow | None] = relationship(back_populates="runs")


class AuditRow(Base):
    """Insert-only. Do not add an update or delete method for this table."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    from_state: Mapped[str] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text)
    accepted: Mapped[bool] = mapped_column(default=False)
    message: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime)

    case: Mapped[CaseRow] = relationship(back_populates="audits")


TABLES: tuple[Type[Base], ...] = (CaseRow, RunRow, AuditRow)
