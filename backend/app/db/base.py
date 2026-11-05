from enum import StrEnum

from sqlalchemy import CheckConstraint, MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names, so Alembic migrations can reference (and drop) them reliably.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def enum_check(column: str, enum: type[StrEnum], name: str) -> CheckConstraint:
    """TEXT column restricted to an enum's values (DECISIONS #015: no native PG enums)."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=name)
