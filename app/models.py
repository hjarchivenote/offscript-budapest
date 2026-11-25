from datetime import date

from sqlalchemy import JSON, CheckConstraint, Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DatabaseState(Base):
    __tablename__ = "database_state"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    schema_version: Mapped[int]
    catalogue_digest: Mapped[str] = mapped_column(String(64))


class Venue(Base):
    __tablename__ = "venues"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    location: Mapped[str] = mapped_column(String(200))
    website: Mapped[str] = mapped_column(String(500))


class Activity(Base):
    __tablename__ = "activities"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venues.id"))
    title: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(20), default="service")
    category: Mapped[str] = mapped_column(String(30))
    details: Mapped[dict] = mapped_column(JSON)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    day: Mapped[date] = mapped_column(Date)
    selected_candidate: Mapped[int | None] = mapped_column(nullable=True)


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (UniqueConstraint("plan_id", "visitor_hash"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    visitor_hash: Mapped[str] = mapped_column(String(64))
    nickname: Mapped[str] = mapped_column(String(40))
    is_host: Mapped[bool] = mapped_column(default=False)


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("plan_id", "activity_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    activity_id: Mapped[str] = mapped_column(ForeignKey("activities.id"))
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint("candidate_id", "participant_id"),
        CheckConstraint("choice IN ('yes', 'maybe', 'no')"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"))
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    choice: Mapped[str] = mapped_column(String(5))


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("activity_id", "visitor_hash"),
        CheckConstraint("rating BETWEEN 1 AND 5"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[str] = mapped_column(ForeignKey("activities.id"))
    visitor_hash: Mapped[str] = mapped_column(String(64))
    nickname: Mapped[str] = mapped_column(String(40))
    rating: Mapped[int]
    text: Mapped[str] = mapped_column(String(1500))
    visited_on: Mapped[date] = mapped_column(Date)
