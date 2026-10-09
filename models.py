from datetime import datetime, timezone

from sqlalchemy import Column, Date, DateTime, String

from database import Base


class Certificate(Base):
    __tablename__ = "certificates"

    id = Column(String(36), primary_key=True)
    candidate_name = Column(String(40), nullable=False)
    title = Column(String(200), nullable=False)
    certificate_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, default="generated")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )