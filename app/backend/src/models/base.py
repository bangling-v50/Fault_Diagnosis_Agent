from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from sqlalchemy import BigInteger, DateTime, Identity, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JsonObject = dict[str, Any]
BigIntPrimaryKey = Annotated[
    int,
    mapped_column(BigInteger, Identity(), primary_key=True),
]
CreatedAt = Annotated[
    datetime,
    mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
]


class Base(DeclarativeBase):
    """Declarative base shared by every FaDia business model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Server-managed creation and modification timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
