"""Flask app + routes: a login-protected dashboard with six daily post
options (three for Instagram, three for LinkedIn) that Dr. Ike can approve,
change, or redo — each publishing to its own platform. An Archive tab shows
everything published, with Instagram analytics per post."""
import functools
import hmac
import logging
import uuid
from datetime import timedelta

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request,
    session as web_session, url_for,
)

import analytics
import config
from db import SessionLocal, init_db
from generator import CATEGORIES, describe_photo, generate_post
from imagegen import prepare_photo, render_card
from learning import distill_lesson
from models import Campaign, Lesson, Photo, Post, Publication, utcnow
from publisher import PLATFORMS, PLATFORM_LABELS, get_publisher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # photo uploads

config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
init_db()


@app.teardown_appcontext
def _remove_session(exc=None):
    SessionLocal.remove()


# ---------------------------------------------------------------- auth

def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not web_session.get("authed"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.get("/login")
def login():
    if web_session.get("authed"):
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.post("/login")
def login_submit():
    password = request.form.get("password", "")
    if not config.DASHBOARD_PASSWORD:
        flash("DASHBOARD_PASSWORD is not configured on the server.", "error")
        return redirect(url_for("login"))
    if not hmac.compare_digest(password, config.DASHBOARD_PASSWORD):
        app.logger.warning("Failed dashboard login from %s", request.remote_addr)
        flash("Wrong password.", "error")
        return redirect(url_for("login"))
    web_session["authed"] = True
    web_session.permanent = True
    return redirect(url_for("dashboard"))


@app.get("/logout")
def logout():
    web_session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- learning

def _active_lessons(category=None) -> list[str]:
    q = SessionLocal().query(Lesson).order_by(Lesson.created_at.desc())
    lessons = [l for l in q.limit(40)
               if l.category is None or category is None or l.category == category]
    return [l.text for l in lessons[:12]]


def _top_performers(category) -> list[tuple[int, str]]:
    """Best-performing published posts: top 2 in-category + top 1 overall.

    Metrics are refreshed quietly here (no analytics UI) so performance
    keeps steering generation.
    """
    session = SessionLocal()
    posts = (
        session.query(Post)
        .filter(Post.status == "published")
        .order_by(Post.published_at.desc())
        .limit(60)
        .all()
    )
    refreshed = 0
    for post in posts:
        for pub in post.publications:
            if refreshed >= 10:
                break
            if pub.status == "published" and analytics.is_stale(pub):
                try:
                    if analytics.refresh_publication(session, pub):
                        refreshed += 1
                except Exception:
                    app.logger.exception("Metrics refresh failed for %s", pub.id)
    scored = []
    for post in posts:
        scores = [p.engagement_score for p in post.publications if p.engagement_score]
        if scores:
            scored.append((sum(scores), post))
    scored.sort(key=lambda item: item[0], reverse=True)

    picks, seen = [], set()
    for total, post in scored:
        if post.category == category and len([1 for _, p in picks if p.category == category]) < 2:
            picks.append((total, post)); seen.add(post.id)
    for total, post in scored:
        if post.id not in seen:
            picks.append((total, post))
            break
    return [(total, post.caption) for total, post in picks[:3]]


def _record_correction(kind, post, before, after=None, note=None):
    """Distill a correction into a lesson; never let this break the main flow."""
    try:
        existing = _active_lessons()
        lesson_text = distill_lesson(kind, post.category, before=before,
                                     after=after or "", note=note or "",
                                     existing=existing)
        if lesson_text:
            session = SessionLocal()
            session.add(Lesson(category=post.category, text=lesson_text, source=kind))
            session.commit()
            flash(f'Learned: "{lesson_text}"', "ok")
    except Exception:
        app.logger.exception("Lesson distillation failed (%s, post %s)", kind, post.id)


# ---------------------------------------------------------------- campaigns

CAMPAIGN_PRESETS = {
    "book": {
        "label": "Promote the book",
        "instruction": (
            'Dr. Ike is promoting his book, "Dr. Ike\'s Triangle of Leadership:'
            ' How to Attract, Move, and Scale People". Lean on the book\'s ideas'
            " and signature lines, mention the book by name when it fits, and"
            " where natural close with a soft invitation to grab a copy."
        ),
    },
    "speaking": {
        "label": "Promote speaking, coaching & consulting",
        "instruction": (
            "Dr. Ike is booking speaking engagements, leadership coaching, and"
            " advisory/consulting work through Iroko Lifesciences Advisory."
            " Where natural, close with an invitation to bring him in to speak"
            " at an event, or to work with him on leadership or biopharma"
            " strategy (his LinkedIn profile is the way to reach him)."
        ),
    },
}

CAMPAIGN_DURATIONS = {7: "1 week", 14: "2 weeks", 30: "1 month", 60: "2 months"}


def _current_campaign():
    """The most recent campaign that hasn't been ended early, if any.
    (It may already be past its end date — callers check is_active.)"""
    return (
        SessionLocal().query(Campaign)
        .filter(Campaign.ended_at.is_(None))
        .order_by(Campaign.starts_at.desc())
        .first()
    )


def _active_campaign_instruction() -> str | None:
    campaign = _current_campaign()
    return campaign.instruction if campaign and campaign.is_active else None


@app.post("/campaign/start")
@login_required
def start_campaign():
    kind = request.form.get("kind", "")
    try:
        days = int(request.form.get("days", "30"))
    except ValueError:
        days = 30
    days = days if days in CAMPAIGN_DURATIONS else 30

    if kind in CAMPAIGN_PRESETS:
        label = CAMPAIGN_PRESETS[kind]["label"]
        instruction = CAMPAIGN_PRESETS[kind]["instruction"]
    elif kind == "custom":
        custom = (request.form.get("custom") or "").strip()
        if not custom:
            flash("Describe the custom focus first.", "error")
            return redirect(url_for("dashboard"))
        label = custom if len(custom) <= 80 else custom[:77] + "…"
        instruction = custom
    else:
        flash("Pick a posting focus first.", "error")
        return redirect(url_for("dashboard"))

    session = SessionLocal()
    session.query(Campaign).filter(Campaign.ended_at.is_(None)).update(
        {"ended_at": utcnow()}
    )
    campaign = Campaign(kind=kind, label=label, instruction=instruction,
                        ends_at=utcnow() + timedelta(days=days))
    session.add(campaign)
    session.commit()
    flash(f'Posting focus set: "{label}" for {CAMPAIGN_DURATIONS[days]}. '
          "Every new post will carry it until it ends.", "ok")
    return redirect(url_for("dashboard"))


@app.post("/campaign/end")
@login_required
def end_campaign():
    session = SessionLocal()
    ended = session.query(Campaign).filter(Campaign.ended_at.is_(None)).update(
        {"ended_at": utcnow()}
    )
    session.commit()
    flash("Posting focus ended — new posts go back to the normal mix."
          if ended else "No posting focus was running.", "ok")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------- generation

def _available_photos():
    return (
        SessionLocal().query(Photo)
        .filter_by(status="available")
        .order_by(Photo.created_at.asc())
        .limit(12)
        .all()
    )


def _create_pending_post(category, source_theme=None, post_group_id=None,
                         revision_of=None, avoid_captions=None) -> Post:
    """generate_post -> library photo or rendered card -> insert pending row.

    Claude sees the photo library and picks a photo when one genuinely fits;
    otherwise a branded quote card is rendered. A picked photo is reserved so
    no other draft can take it.
    """
    photos = _available_photos()
    content = generate_post(
        category, source_theme,
        avoid_captions=avoid_captions,
        lessons=_active_lessons(category),
        top_performers=_top_performers(category),
        photos=[(i, p.description) for i, p in enumerate(photos)],
        campaign=_active_campaign_instruction(),
    )
    post_id = str(uuid.uuid4())

    choice = content.get("photo")
    photo = photos[choice] if choice is not None and 0 <= choice < len(photos) else None
    if photo is not None:
        image_url = f"{config.APP_BASE_URL}/static/media/{photo.filename}"
    else:
        filename = f"{post_id}.jpg"
        render_card(content["image_text"], str(config.MEDIA_DIR / filename))
        image_url = f"{config.APP_BASE_URL}/static/media/{filename}"

    post = Post(
        id=post_id,
        post_group_id=post_group_id or post_id,
        platform=CATEGORIES[category]["platform"],
        category=category,
        caption=content["caption"],
        hashtags=content["hashtags"],
        image_text=content["image_text"],
        image_url=image_url,
        source_theme=source_theme,
        revision_of=revision_of,
    )
    session = SessionLocal()
    session.add(post)
    if photo is not None:
        photo.status = "reserved"
        photo.post_id = post_id
    session.commit()
    return post


def _release_photos(session, post) -> None:
    """A draft was discarded/failed — its reserved photo returns to the library."""
    session.query(Photo).filter_by(post_id=post.id, status="reserved").update(
        {"status": "available", "post_id": None}
    )
    session.commit()


def _retire_photos(session, post) -> None:
    """The post was published — its photo is used once and retired."""
    session.query(Photo).filter_by(post_id=post.id, status="reserved").update(
        {"status": "used", "used_at": utcnow()}
    )
    session.commit()


def _pending_posts():
    return (
        SessionLocal().query(Post)
        .filter_by(status="pending")
        .order_by(Post.created_at.asc())
        .all()
    )


def _retire_legacy_drafts():
    """Pending drafts from a category set that no longer exists are rejected
    (their photos released) so the dashboard only shows current slots."""
    session = SessionLocal()
    for post in _pending_posts():
        if post.category not in CATEGORIES:
            post.status = "rejected"
            session.commit()
            _release_photos(session, post)
            app.logger.info("Retired legacy pending draft %s (%s)", post.id, post.category)


def _top_up_options(theme=None):
    """Generate one pending post per category that isn't already covered.

    Each draft is told about the existing ones so the options differ.
    Returns (created, errors).
    """
    _retire_legacy_drafts()
    pending = _pending_posts()
    covered = {p.category for p in pending}
    # photo posts don't occupy a category slot
    missing = [c for c in CATEGORIES if c not in covered]
    avoid = [p.caption for p in pending]
    created, errors = [], []
    for category in missing:
        try:
            post = _create_pending_post(category, theme, avoid_captions=avoid)
            created.append(post)
            avoid.append(post.caption)
        except Exception as e:  # keep whatever already succeeded
            app.logger.exception("Generating the %s option failed", category)
            errors.append(f"{CATEGORIES[category]['label']}: {e}")
    return created, errors


@app.post("/generate")
def generate():
    """Cron entry point: top the dashboard up to DAILY_POST_COUNT pending options."""
    if config.GENERATE_SECRET:
        if request.headers.get("X-Generate-Secret") != config.GENERATE_SECRET:
            abort(401)
    else:
        app.logger.warning("GENERATE_SECRET is not set — /generate is unprotected. Set it in production.")

    theme = None
    if request.is_json:
        theme = (request.get_json(silent=True) or {}).get("theme")
    theme = theme or request.form.get("theme") or request.args.get("theme")

    created, errors = _top_up_options(theme)
    return jsonify({
        "created": [p.id for p in created],
        "pending_total": len(_pending_posts()),
        "errors": errors,
    }), (201 if created or not errors else 500)


@app.post("/photos/upload")
@login_required
def upload_photos():
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        flash("Choose at least one photo.", "error")
        return redirect(url_for("dashboard"))

    session = SessionLocal()
    added, errors = 0, 0
    for file in files[:10]:
        filename = f"{uuid.uuid4()}.jpg"
        path = config.MEDIA_DIR / filename
        try:
            prepare_photo(file.stream, str(path))
            description = describe_photo(str(path))
            session.add(Photo(filename=filename, description=description))
            session.commit()
            added += 1
        except Exception:
            app.logger.exception("Photo upload failed (%s)", file.filename)
            path.unlink(missing_ok=True)
            errors += 1

    if added:
        flash(f"Added {added} photo{'s' if added != 1 else ''} to the library — "
              "new posts will use them when they fit.", "ok")
    if errors:
        flash(f"{errors} photo{'s' if errors != 1 else ''} couldn't be read — "
              "use JPEG, PNG, or WebP.", "error")
    return redirect(url_for("dashboard"))


@app.post("/photos/<photo_id>/delete")
@login_required
def delete_photo(photo_id):
    session = SessionLocal()
    photo = session.get(Photo, photo_id)
    if photo is None:
        abort(404)
    if photo.status == "reserved":
        flash("That photo is in a pending draft — redo or publish the draft first.", "error")
    elif photo.status == "used":
        flash("That photo was already published, so it stays in the record.", "error")
    else:
        (config.MEDIA_DIR / photo.filename).unlink(missing_ok=True)
        session.delete(photo)
        session.commit()
        flash("Photo removed from the library.", "ok")
    return redirect(url_for("dashboard"))


@app.post("/generate-batch")
@login_required
def generate_batch():
    created, errors = _top_up_options(request.form.get("theme") or None)
    if created:
        flash(f"Generated {len(created)} new option{'s' if len(created) != 1 else ''}.", "ok")
    if errors:
        flash(f"Generation failed: {errors[0]}", "error")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------- dashboard

@app.get("/")
@login_required
def dashboard():
    session = SessionLocal()
    order = {key: i for i, key in enumerate(CATEGORIES)}
    pending = sorted(_pending_posts(), key=lambda p: order.get(p.category, 99))
    lessons = (
        session.query(Lesson).order_by(Lesson.created_at.desc()).limit(20).all()
    )
    photos = (
        session.query(Photo).order_by(Photo.created_at.desc()).limit(60).all()
    )
    campaign = _current_campaign()
    return render_template(
        "dashboard.html",
        pending_by_platform={
            platform: [p for p in pending if p.platform == platform]
            for platform in PLATFORMS
        },
        pending_total=len(pending),
        lessons=lessons,
        photos=photos,
        platform_labels=PLATFORM_LABELS,
        categories=CATEGORIES,
        daily_count=len(CATEGORIES),
        today=utcnow().strftime("%A, %B %-d"),
        campaign=campaign if campaign and campaign.is_active else None,
        expired_campaign=campaign if campaign and not campaign.is_active else None,
        campaign_presets=CAMPAIGN_PRESETS,
        campaign_durations=CAMPAIGN_DURATIONS,
    )


@app.get("/archive")
@login_required
def archive():
    """Everything published, newest first, with per-post Instagram analytics.

    Stale Instagram metrics are refreshed quietly (a few per page load) so the
    numbers stay current without a background worker.
    """
    session = SessionLocal()
    posts = (
        session.query(Post)
        .filter(Post.status == "published")
        .order_by(Post.published_at.desc())
        .limit(200)
        .all()
    )
    refreshed = 0
    for post in posts:
        for pub in post.publications:
            if refreshed >= 10:
                break
            if pub.status == "published" and analytics.is_stale(pub):
                try:
                    if analytics.refresh_publication(session, pub):
                        refreshed += 1
                except Exception:
                    app.logger.exception("Metrics refresh failed for %s", pub.id)
    return render_template(
        "archive.html",
        posts=posts,
        platform_labels=PLATFORM_LABELS,
        categories=CATEGORIES,
        today=utcnow().strftime("%A, %B %-d"),
    )


def _get_post_or_404(post_id: str) -> Post:
    post = SessionLocal().get(Post, post_id)
    if post is None:
        abort(404)
    return post


def _post_text(caption, hashtags) -> str:
    return f"{caption}\n\nHashtags: {hashtags or '(none)'}"


def _apply_edits(session, post) -> str | None:
    """Apply form edits. Returns the pre-edit text if something changed."""
    caption = (request.form.get("caption") or "").strip()
    if post.status != "pending" or not caption:
        return None
    hashtags = (request.form.get("hashtags") or "").strip()
    before = _post_text(post.caption, post.hashtags)
    if _post_text(caption, hashtags) == before:
        return None
    post.caption = caption
    post.hashtags = hashtags
    session.commit()
    return before


@app.post("/post/<post_id>/save")
@login_required
def save(post_id):
    session = SessionLocal()
    post = _get_post_or_404(post_id)
    if post.status != "pending":
        flash("That post was already actioned.", "error")
    else:
        before = _apply_edits(session, post)
        flash("Changes saved.", "ok")
        if before:
            _record_correction("edit", post, before,
                               after=_post_text(post.caption, post.hashtags))
    return redirect(url_for("dashboard"))


@app.post("/post/<post_id>/redo")
@login_required
def redo(post_id):
    session = SessionLocal()
    post = _get_post_or_404(post_id)
    if post.status != "pending":
        flash("That post was already actioned.", "error")
        return redirect(url_for("dashboard"))

    post.status = "rejected"
    session.commit()
    _release_photos(session, post)  # its photo goes back to the library

    note = (request.form.get("note") or "").strip() or None
    if note:
        _record_correction("redo", post, post.caption, note=note)

    # regenerate within the same category; legacy rows fall back to an uncovered one
    category = post.category
    if category not in CATEGORIES:
        covered = {p.category for p in _pending_posts()}
        category = next((c for c in CATEGORIES if c not in covered), next(iter(CATEGORIES)))
    avoid = [p.caption for p in _pending_posts()] + [post.caption]
    try:
        _create_pending_post(
            category,
            source_theme=note or post.source_theme,
            post_group_id=post.post_group_id,
            revision_of=post.id,
            avoid_captions=avoid,
        )
        flash(f"New {CATEGORIES[category]['label']} option generated.", "ok")
    except Exception as e:
        app.logger.exception("Redo generation failed")
        flash(f"Couldn't generate a replacement: {e}", "error")
    return redirect(url_for("dashboard"))


@app.post("/post/<post_id>/publish")
@login_required
def publish(post_id):
    session = SessionLocal()
    post = _get_post_or_404(post_id)

    # each post was written for exactly one platform
    platform = post.platform if post.platform in PLATFORMS else "instagram"
    label = PLATFORM_LABELS[platform]

    before = _apply_edits(session, post)
    if before:
        _record_correction("edit", post, before,
                           after=_post_text(post.caption, post.hashtags))

    # atomic claim: only one request can move pending -> publishing
    claimed = (
        session.query(Post)
        .filter_by(id=post.id, status="pending")
        .update({"status": "publishing", "approved_at": utcnow()})
    )
    session.commit()
    if not claimed:
        flash("That post was already actioned.", "error")
        return redirect(url_for("dashboard"))

    session.refresh(post)
    pub = Publication(post_id=post.id, platform=platform)
    session.add(pub)
    session.commit()
    error = None
    try:
        external_id = get_publisher(platform).publish(post)
        pub.status = "published"
        pub.external_id = external_id
        pub.published_at = utcnow()
        if platform == "instagram":
            post.ig_media_id = external_id
    except Exception as e:
        app.logger.exception("Publishing post %s to %s failed", post.id, platform)
        pub.status = "failed"
        pub.error = str(e)
        error = str(e)
    session.commit()

    post.status = "failed" if error else "published"
    post.published_at = None if error else utcnow()
    post.error = f"{label}: {error}" if error else None
    session.commit()
    if error:
        _release_photos(session, post)  # nothing went out — photo returns to the library
        flash(f"Publishing to {label} failed: {error[:200]}", "error")
    else:
        _retire_photos(session, post)   # a published photo is used exactly once
        flash(f"Published to {label}.", "ok")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------- lessons

@app.post("/lessons/<lesson_id>/delete")
@login_required
def delete_lesson(lesson_id):
    session = SessionLocal()
    lesson = session.get(Lesson, lesson_id)
    if lesson is not None:
        session.delete(lesson)
        session.commit()
        flash("Lesson removed — it will no longer influence new posts.", "ok")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------- cron helper

def generate_scheduled(theme=None):
    """Entry point for a Render Cron Job:
    python -c "import app; app.generate_scheduled()"
    """
    with app.app_context():
        created, errors = _top_up_options(theme)
        print(f"Created {len(created)} option(s); dashboard: {config.APP_BASE_URL}/")
        for e in errors:
            print(f"GENERATION ERROR: {e}")


if __name__ == "__main__":
    app.run(debug=True)
