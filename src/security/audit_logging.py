from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import func, DateTime, String
from datetime import datetime
from uuid import UUID


class Auditable:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now()
    )

    created_by: Mapped[str] = mapped_column(String(36), nullable=False, default="System")
    last_updated_by: Mapped[str] = mapped_column(String(36), nullable=False, default="System")


def apply_audit_fields(audit: Auditable, user_id: Optional[UUID] = None, is_create: bool = False):
    if user_id is not None:
        if is_create:
            audit.created_by = str(user_id)
        audit.last_updated_by = str(user_id)