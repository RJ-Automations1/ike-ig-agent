"""SQLAlchemy models: posts, per-platform publications, and ig_credentials."""
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from db import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Post(Base):
    __tablename__ = "posts"

    id = Column(String(36), primary_key=True, default=new_uuid)
    # groups a post with its redo revisions; defaults to the post's own id
    post_group_id = Column(String(36), nullable=False)
    # instagram | linkedin — the one platform this post was written for
    platform = Column(String(32), nullable=False, default="instagram")
    # pending | approved | publishing | published | failed | rejected
    status = Column(String(20), nullable=False, default="pending", index=True)
    # key from generator.CATEGORIES; null/legacy keys on old rows
    category = Column(String(32), nullable=True)
    caption = Column(Text, nullable=False)
    hashtags = Column(Text, nullable=False, default="")
    image_url = Column(Text, nullable=False)   # public HTTPS URL to the rendered JPEG
    image_text = Column(Text, nullable=False)  # the line rendered on the card
    source_theme = Column(Text, nullable=True)
    review_token = Column(
        String(64), unique=True, nullable=False,
        default=lambda: secrets.token_urlsafe(32),
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    ig_media_id = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    revision_of = Column(String(36), nullable=True)

    publications = relationship(
        "Publication", back_populates="post", order_by="Publication.created_at"
    )


class Publication(Base):
    """One publish attempt of a post to one platform."""

    __tablename__ = "publications"

    id = Column(String(36), primary_key=True, default=new_uuid)
    post_id = Column(String(36), ForeignKey("posts.id"), nullable=False, index=True)
    platform = Column(String(32), nullable=False)  # instagram | linkedin
    # publishing | published | failed
    status = Column(String(20), nullable=False, default="publishing")
    external_id = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_at = Column(DateTime(timezone=True), nullable=True)

    # analytics (fetched from the platform APIs, entered manually for LinkedIn,
    # or mocked in dev)
    impressions = Column(Integer, nullable=True)
    likes = Column(Integer, nullable=True)
    comments = Column(Integer, nullable=True)
    shares = Column(Integer, nullable=True)
    metrics_source = Column(String(10), nullable=True)  # api | manual | mock
    metrics_updated_at = Column(DateTime(timezone=True), nullable=True)

    post = relationship("Post", back_populates="publications")

    @property
    def has_metrics(self) -> bool:
        return any(v is not None for v in (self.impressions, self.likes,
                                           self.comments, self.shares))

    @property
    def engagement_score(self):
        """Blend: engagement weighted first, impressions as the tiebreaker."""
        if not self.has_metrics:
            return None
        return round((self.likes or 0) + 2 * (self.comments or 0)
                     + 3 * (self.shares or 0) + (self.impressions or 0) / 100)


class Photo(Base):
    """A library photo. Generated posts pick from these when one fits;
    each photo is used at most once."""

    __tablename__ = "photos"

    id = Column(String(36), primary_key=True, default=new_uuid)
    filename = Column(Text, nullable=False)          # in static/media/
    description = Column(Text, nullable=False)       # AI description, used for matching
    # available | reserved (in a pending draft) | used (published)
    status = Column(String(20), nullable=False, default="available", index=True)
    post_id = Column(String(36), nullable=True)      # draft/post currently holding it
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    used_at = Column(DateTime(timezone=True), nullable=True)


class Lesson(Base):
    """A durable writing rule distilled from one of Dr. Ike's corrections."""

    __tablename__ = "lessons"

    id = Column(String(36), primary_key=True, default=new_uuid)
    category = Column(String(32), nullable=True)  # null = applies to every category
    text = Column(Text, nullable=False)
    source = Column(String(20), nullable=False, default="edit")  # edit | redo
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class IgCredentials(Base):
    __tablename__ = "ig_credentials"

    id = Column(Integer, primary_key=True)
    access_token = Column(Text, nullable=False)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)
    ig_user_id = Column(Text, nullable=False)


def seed_credentials(session) -> None:
    """If ig_credentials is empty, seed it from IG_USER_ID / IG_ACCESS_TOKEN."""
    import config

    if session.get(IgCredentials, 1) is None and config.IG_USER_ID and config.IG_ACCESS_TOKEN:
        session.add(
            IgCredentials(
                id=1,
                access_token=config.IG_ACCESS_TOKEN,
                ig_user_id=config.IG_USER_ID,
                token_expires_at=utcnow() + timedelta(days=60),
            )
        )
        session.commit()
