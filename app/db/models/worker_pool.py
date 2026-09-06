"""One execution gate per environment, shared by the API and Pull workers."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class WorkerPoolControl(Base):
    __tablename__ = "worker_pool_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target: Mapped[int] = mapped_column(Integer, default=0)
    command: Mapped[str] = mapped_column(String(32), default="")
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    idle_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
