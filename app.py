"""Flask app + routes: a login-protected dashboard with six daily post
options (three for Instagram, three for LinkedIn) that Dr. Ike can approve,
change, or redo — each publishing to its own platform. An Archive tab shows
everything published, with Instagram analytics per post."""
import functools
import hmac
import io
import logging
import re
import time
import uuid
from datetime import timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request,
    session as web_session, url_for,
)
from sqlalchemy import func

import analytics
import config
from db import SessionLocal, init_db
from generator import (
    ALL_CATEGORIES, CATEGORIES, CUSTOM_CATEGORIES, describe_photo,
    fetch_url_via_claude, generate_custom_post, generate_post, pick_photo,
    summarize_document,
)
from imagegen import prepare_photo, render_card
from learning import distill_lesson
from models import (
    Campaign, Document, IgCredentials, Lesson, Photo, Post, Publication,
    utcnow,
)
from publisher import PLATFORMS, PLATFORM_LABELS, get_publisher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # photo uploads

config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
init_db()

# Dr. Ike lives in Eastern time — every timestamp he sees is ET, not UTC
EASTERN = ZoneInfo("America/New_York")


def _as_utc(dt):
    """SQLite hands back naive datetimes; everything here is stored as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@app.template_filter("et")
def fmt_eastern(dt, fmt="%b %-d, %-I:%M %p ET"):
    if dt is None:
        return ""
    return _as_utc(dt).astimezone(EASTERN).strftime(fmt)


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


def _match_login(password: str):
    """Each person has their own password; the box stays a single field.
    Returns (role, name) or (None, None)."""
    for role, name, secret in (
        ("admin", "Vernon", config.ADMIN_PASSWORD),
        ("client", "Dr. Ike", config.DASHBOARD_PASSWORD),
        ("client", "Erica", config.ERICA_PASSWORD),
    ):
        if secret and hmac.compare_digest(password, secret):
            return role, name
    return None, None


def _is_admin() -> bool:
    return web_session.get("role") == "admin"


@app.post("/login")
def login_submit():
    password = request.form.get("password", "")
    if not (config.DASHBOARD_PASSWORD or config.ADMIN_PASSWORD or config.ERICA_PASSWORD):
        flash("No dashboard passwords are configured on the server.", "error")
        return redirect(url_for("login"))
    role, name = _match_login(password)
    if role is None:
        app.logger.warning("Failed dashboard login from %s", request.remote_addr)
        time.sleep(1)  # blunt but effective brake on password guessing
        flash("Wrong password.", "error")
        return redirect(url_for("login"))
    web_session["authed"] = True
    web_session["role"] = role
    web_session["user"] = name
    web_session.permanent = True
    app.logger.info("%s signed in", name)
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


def _refresh_stale_metrics(session, posts, limit=10) -> None:
    """Quietly refresh a few stale publications per page load, so numbers stay
    current without a background worker."""
    refreshed = 0
    for post in posts:
        for pub in post.publications:
            if pub.status == "published" and analytics.is_stale(pub):
                try:
                    if analytics.refresh_publication(session, pub):
                        refreshed += 1
                except Exception:
                    app.logger.exception("Metrics refresh failed for %s", pub.id)
            if refreshed >= limit:
                return


def _top_performers(category) -> list[tuple[int, str]]:
    """Best-performing published posts: top 2 in-category + top 1 overall.

    Mock metrics are excluded — until real publishing goes live there is no
    performance data, and fake numbers must not steer what gets written.
    """
    session = SessionLocal()
    posts = (
        session.query(Post)
        .filter(Post.status == "published")
        .order_by(Post.published_at.desc())
        .limit(60)
        .all()
    )
    _refresh_stale_metrics(session, posts)
    scored = []
    for post in posts:
        scores = [p.engagement_score for p in post.publications
                  if p.engagement_score and p.metrics_source != "mock"]
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
        library=_library_block(),
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


def _recycle_photos() -> None:
    """Photos published more than PHOTO_REUSE_DAYS ago quietly return to the
    library — nobody remembers a photo from two months back, and this keeps
    Vernon's finite photo supply from draining permanently."""
    session = SessionLocal()
    cutoff = utcnow() - timedelta(days=config.PHOTO_REUSE_DAYS)
    recycled = (
        session.query(Photo)
        .filter(Photo.status == "used", Photo.used_at.isnot(None),
                Photo.used_at < cutoff)
        .update({"status": "available", "post_id": None, "used_at": None},
                synchronize_session=False)
    )
    if recycled:
        session.commit()
        app.logger.info("Recycled %d photo(s) back into the library", recycled)


def _pending_posts():
    return (
        SessionLocal().query(Post)
        .filter_by(status="pending")
        .order_by(Post.created_at.asc())
        .all()
    )


def _saved_posts():
    """Drafts Dr. Ike set aside — they keep their slot-free life until he
    publishes or removes them."""
    return (
        SessionLocal().query(Post)
        .filter_by(status="saved")
        .order_by(Post.created_at.desc())
        .all()
    )


def _retire_legacy_drafts():
    """Pending drafts from a category set that no longer exists are rejected
    (their photos released) so the dashboard only shows current slots."""
    session = SessionLocal()
    for post in _pending_posts():
        if post.category not in ALL_CATEGORIES:  # custom drafts stay
            post.status = "rejected"
            session.commit()
            _release_photos(session, post)
            app.logger.info("Retired legacy pending draft %s (%s)", post.id, post.category)


def _top_up_options(theme=None):
    """Generate one pending post per category that isn't already covered.

    Each draft is told about the existing ones so the options differ.
    Returns (created, errors).
    """
    _recycle_photos()
    _retire_legacy_drafts()
    pending = _pending_posts()
    covered = {p.category for p in pending}
    # photo posts don't occupy a category slot
    missing = [c for c in CATEGORIES if c not in covered]
    # saved drafts don't hold a slot, but new options should still differ from them
    avoid = [p.caption for p in pending] + [p.caption for p in _saved_posts()]
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
    """Cron entry point: top the dashboard up to one pending option per category."""
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


def _backup_original(file, data: bytes) -> None:
    """Copy the untouched upload into PHOTOS_BACKUP_DIR (Vernon's own photo
    archive). Never let a backup problem break the upload itself."""
    if config.PHOTOS_BACKUP_DIR is None:
        return
    try:
        config.PHOTOS_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        name = Path(file.filename).name or "photo"
        target = config.PHOTOS_BACKUP_DIR / name
        if target.exists():
            stem, suffix = target.stem, target.suffix
            target = target.with_name(f"{stem}-{uuid.uuid4().hex[:8]}{suffix}")
        target.write_bytes(data)
    except Exception:
        app.logger.exception("Backing up %s to the photos folder failed", file.filename)


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
            data = file.read()
            _backup_original(file, data)
            prepare_photo(io.BytesIO(data), str(path))
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


# ---------------------------------------------------------------- data library

DOC_TEXT_LIMIT = 20000  # characters of extracted text kept per document


def _extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages[:40]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(pages).strip()
    if not text:
        raise ValueError("no extractable text in the PDF")
    return text[:DOC_TEXT_LIMIT]


def _fetch_link_text(url: str) -> str:
    import html as html_lib
    resp = requests.get(url, timeout=30, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
    })
    resp.raise_for_status()
    html = resp.text
    # crude but dependency-free: drop scripts/styles/tags, collapse whitespace
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    text = html_lib.unescape(re.sub(r"\s+", " ", html)).strip()
    if len(text) < 200:
        raise ValueError("the page had almost no readable text")
    return text[:DOC_TEXT_LIMIT]


def _recent_documents(limit=12):
    return (
        SessionLocal().query(Document)
        .order_by(Document.created_at.desc())
        .limit(limit)
        .all()
    )


def _library_block() -> list[tuple[str, str]]:
    """What daily generation sees: the five most recent document summaries."""
    return [(d.title, d.summary) for d in _recent_documents(limit=5)]


@app.post("/library/add")
@login_required
def add_document():
    """Add an article, link, or PDF to the data library. Images belong in the
    photo half; everything else lands here as searchable reference text."""
    file = request.files.get("doc_file")
    pasted = (request.form.get("doc_text") or "").strip()

    try:
        meta = None
        if file and file.filename:
            data = file.read()
            if not (file.filename.lower().endswith(".pdf") or data[:5] == b"%PDF-"):
                flash("Upload PDFs here — pictures go in the photo box on the left.", "error")
                return redirect(url_for("dashboard"))
            content = _extract_pdf_text(data)
            filename = f"doc-{uuid.uuid4()}.pdf"
            (config.MEDIA_DIR / filename).write_bytes(data)
            kind, url, hint = "pdf", None, file.filename
        elif pasted and re.fullmatch(r"https?://\S+", pasted):
            kind, url, filename, hint = "link", pasted, None, pasted
            try:
                content = _fetch_link_text(pasted)
            except Exception:
                # many sites block requests from cloud-server IPs — have
                # Claude fetch the page from its side instead
                app.logger.info("Direct fetch blocked for %s — using web_fetch fallback", pasted)
                fetched = fetch_url_via_claude(pasted)
                content = fetched["content"]
                meta = {"title": fetched["title"], "summary": fetched["summary"]}
        elif pasted:
            content = pasted[:DOC_TEXT_LIMIT]
            kind, url, filename, hint = "text", None, None, "pasted text"
        else:
            flash("Paste an article, a link, or choose a PDF first.", "error")
            return redirect(url_for("dashboard"))

        if meta is None:
            meta = summarize_document(content, source_hint=hint)
    except Exception as e:
        app.logger.exception("Library add failed")
        flash("Couldn't read that link or material — the site may be blocking "
              f"automated readers. Try pasting the article text instead. ({e})", "error")
        return redirect(url_for("dashboard"))

    session = SessionLocal()
    session.add(Document(kind=kind, title=meta["title"], summary=meta["summary"],
                         content=content, url=url, filename=filename))
    session.commit()
    flash(f'Added to the library: "{meta["title"]}" — the agent can now draw on it.', "ok")
    return redirect(url_for("dashboard"))


@app.post("/documents/<doc_id>/delete")
@login_required
def delete_document(doc_id):
    session = SessionLocal()
    doc = session.get(Document, doc_id)
    if doc is None:
        abort(404)
    if doc.filename:
        (config.MEDIA_DIR / doc.filename).unlink(missing_ok=True)
    session.delete(doc)
    session.commit()
    flash("Removed from the library.", "ok")
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


def _create_custom_post(category, source_text, image_filename,
                        avoid_captions=None, post_group_id=None,
                        revision_of=None) -> Post:
    """One 'Generate a post' draft: Claude writes from the uploaded material,
    the uploaded image (or a rendered quote card) becomes the post image."""
    content = generate_custom_post(
        category,
        source_text=source_text,
        image_path=(str(config.MEDIA_DIR / image_filename) if image_filename else None),
        avoid_captions=avoid_captions,
        lessons=_active_lessons(),
        campaign=_active_campaign_instruction(),
    )
    post_id = str(uuid.uuid4())
    if image_filename:
        image_url = f"{config.APP_BASE_URL}/static/media/{image_filename}"
    else:
        render_card(content["image_text"], str(config.MEDIA_DIR / f"{post_id}.jpg"))
        image_url = f"{config.APP_BASE_URL}/static/media/{post_id}.jpg"
    post = Post(
        id=post_id,
        post_group_id=post_group_id or post_id,
        platform=CUSTOM_CATEGORIES[category]["platform"],
        category=category,
        caption=content["caption"],
        hashtags=content["hashtags"],
        image_text=content["image_text"],
        image_url=image_url,
        source_theme=(source_text or "").strip()[:500] or None,
        revision_of=revision_of,
    )
    session = SessionLocal()
    session.add(post)
    session.commit()
    return post


@app.post("/custom/generate")
@login_required
def custom_generate():
    """The "Generate a post" box: upload an image and/or paste text, get one
    draft per platform (Instagram & Facebook, LinkedIn, X) in his voice."""
    file = request.files.get("source_image")
    text = (request.form.get("source_text") or "").strip()

    # optionally start from a library document — its full text is the source
    doc = None
    if request.form.get("document_id"):
        session = SessionLocal()
        doc = session.get(Document, request.form["document_id"])
        if doc is not None:
            source_parts = [f'LIBRARY MATERIAL — "{doc.title}"'
                            + (f" ({doc.url})" if doc.url else "") + ":\n"
                            + doc.content]
            if text:
                source_parts.append("DIRECTION FROM DR. IKE'S TEAM:\n" + text)
            text = "\n\n".join(source_parts)
            doc.used_count = (doc.used_count or 0) + 1
            session.commit()

    if not (file and file.filename) and not text:
        flash("Add a photo, paste some text, or pick something from the library first.", "error")
        return redirect(url_for("dashboard"))

    image_filename = None
    if file and file.filename:
        try:
            data = file.read()
            _backup_original(file, data)
            image_filename = f"custom-{uuid.uuid4()}.jpg"
            prepare_photo(io.BytesIO(data), str(config.MEDIA_DIR / image_filename))
        except Exception:
            app.logger.exception("Custom upload failed (%s)", file.filename)
            flash("That image couldn't be read — use JPEG, PNG, or WebP.", "error")
            return redirect(url_for("dashboard"))

    avoid = [p.caption for p in _pending_posts()] + [p.caption for p in _saved_posts()]
    created, errors = [], []
    for category in ("custom_ig", "custom_li", "custom_x"):
        try:
            post = _create_custom_post(category, text, image_filename,
                                       avoid_captions=avoid)
            created.append(post)
            avoid.append(post.caption)
        except Exception as e:
            app.logger.exception("Custom generation failed (%s)", category)
            errors.append(f"{PLATFORM_LABELS[CUSTOM_CATEGORIES[category]['platform']]}: {e}")
    if created:
        platforms = ", ".join(PLATFORM_LABELS[p.platform] for p in created)
        flash(f"Wrote {len(created)} post{'s' if len(created) != 1 else ''} from "
              f"your material ({platforms}) — they're with today's options below.", "ok")
    if errors:
        flash(f"Couldn't write every post: {errors[0]}", "error")
    return redirect(url_for("dashboard"))


@app.post("/generate-batch")
@login_required
def generate_batch():
    created, errors = _top_up_options(request.form.get("theme") or None)
    if created:
        flash(f"Generated {len(created)} new option{'s' if len(created) != 1 else ''}.", "ok")
    if errors:
        flash(f"Generation failed: {errors[0]}", "error")
    if not created and not errors:
        flash("All of today's options are already waiting below.", "ok")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------- dashboard

def _system_health(session, photo_counts):
    """The small status strip + any warnings that deserve a banner."""
    items, warnings = [], []

    live = not config.USE_MOCK_PUBLISHER
    items.append({"text": "Publishing: live" if live else "Publishing: test mode",
                  "warn": False})

    if config.LINKEDIN_TOKEN_EXPIRES:
        li_days = (config.LINKEDIN_TOKEN_EXPIRES
                   - utcnow().astimezone(EASTERN).date()).days
        if li_days < 0:
            items.append({"text": "LinkedIn: connection expired", "warn": True})
            warnings.append("The LinkedIn connection has expired — publishing to "
                            "LinkedIn will fail until it is re-authorized.")
        else:
            items.append({"text": f"LinkedIn: {li_days} days left on the connection",
                          "warn": li_days <= 14})
            if li_days <= 14:
                warnings.append(f"The LinkedIn connection expires in {li_days} "
                                f"day{'s' if li_days != 1 else ''} — re-authorize it "
                                "soon so publishing doesn't stop.")

    creds = session.get(IgCredentials, 1)
    if creds is None:
        items.append({"text": "Instagram: not connected yet", "warn": False})
    elif creds.token_expires_at is not None:
        ig_days = (_as_utc(creds.token_expires_at) - utcnow()).days
        items.append({"text": f"Instagram: {max(ig_days, 0)} days left on the connection",
                      "warn": ig_days <= 14})
        if ig_days <= 14:
            warnings.append(f"The Instagram connection expires in {max(ig_days, 0)} "
                            f"day{'s' if ig_days != 1 else ''} — the weekly refresh "
                            "should renew it; check it if this number keeps falling.")
    else:
        items.append({"text": "Instagram: connected", "warn": False})

    photos_low = photo_counts["available"] < 3
    items.append({"text": f"Photos ready: {photo_counts['available']}",
                  "warn": photos_low})
    if photos_low and photo_counts["total"]:
        warnings.append("The photo library is running low — add a batch of photos "
                        "so posts keep leading with real pictures.")

    last = session.query(func.max(Post.created_at)).scalar()
    if last:
        items.append({"text": f"Last draft written {fmt_eastern(last)}", "warn": False})
    return items, warnings


@app.get("/")
@login_required
def dashboard():
    _recycle_photos()
    session = SessionLocal()
    order = {key: i for i, key in enumerate(CATEGORIES)}
    pending = sorted(_pending_posts(), key=lambda p: order.get(p.category, 99))
    lessons = (
        session.query(Lesson).order_by(Lesson.created_at.desc()).limit(20).all()
    )
    # photos are hidden by design — the dashboard only shows how many there are
    counts = dict(session.query(Photo.status, func.count()).group_by(Photo.status).all())
    photo_counts = {status: counts.get(status, 0)
                    for status in ("available", "reserved", "used")}
    photo_counts["total"] = sum(photo_counts.values())
    campaign = _current_campaign()
    is_admin = _is_admin()
    health_items, health_warnings = (
        _system_health(session, photo_counts) if is_admin else ([], [])
    )
    return render_template(
        "dashboard.html",
        pending_by_platform={
            platform: [p for p in pending if p.platform == platform]
            for platform in PLATFORMS
        },
        pending_total=len([p for p in pending if p.category in CATEGORIES]),
        is_admin=is_admin,
        health_items=health_items,
        health_warnings=health_warnings,
        lessons=lessons,
        photo_counts=photo_counts,
        platform_labels=PLATFORM_LABELS,
        categories=ALL_CATEGORIES,
        daily_count=len(CATEGORIES),
        platform_expected={
            platform: sum(1 for c in CATEGORIES.values() if c["platform"] == platform)
            for platform in PLATFORMS
        },
        daily_counts={
            platform: sum(1 for p in pending
                          if p.platform == platform and p.category in CATEGORIES)
            for platform in PLATFORMS
        },
        custom_counts={
            platform: sum(1 for p in pending
                          if p.platform == platform and p.category in CUSTOM_CATEGORIES)
            for platform in PLATFORMS
        },
        today=utcnow().astimezone(EASTERN).strftime("%A, %B %-d"),
        campaign=campaign if campaign and campaign.is_active else None,
        expired_campaign=campaign if campaign and not campaign.is_active else None,
        campaign_presets=CAMPAIGN_PRESETS,
        campaign_durations=CAMPAIGN_DURATIONS,
        library_photos=_available_photos(),
        documents=_recent_documents(),
        posts_with_photo={p.post_id for p in
                          session.query(Photo).filter_by(status="reserved")},
        card_posts={p.id for p in pending if _uses_card(p)},
    )


def _archive_stats(posts) -> dict:
    """Header numbers for the Archive: totals, average engagement, and the
    best post of the last 30 days."""
    stats = {
        "total": len(posts),
        "instagram": sum(1 for p in posts if p.platform == "instagram"),
        "linkedin": sum(1 for p in posts if p.platform == "linkedin"),
        "x": sum(1 for p in posts if p.platform == "x"),
        "avg_score": None, "best_caption": None, "best_score": None,
        "sample": False,
    }
    scores, best = [], None
    cutoff = utcnow() - timedelta(days=30)
    for post in posts:
        for pub in post.publications:
            if pub.status != "published" or not pub.engagement_score:
                continue
            scores.append(pub.engagement_score)
            if pub.metrics_source == "mock":
                stats["sample"] = True
            recent = post.published_at and _as_utc(post.published_at) >= cutoff
            if recent and (best is None or pub.engagement_score > best[0]):
                best = (pub.engagement_score, post.caption)
    if scores:
        stats["avg_score"] = round(sum(scores) / len(scores))
    if best:
        caption = " ".join(best[1].split())
        stats["best_score"] = best[0]
        stats["best_caption"] = caption if len(caption) <= 110 else caption[:107] + "…"
    return stats


@app.get("/archive")
@login_required
def archive():
    """Everything published, newest first, with per-post analytics.

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
    _refresh_stale_metrics(session, posts)
    saved = _saved_posts()
    return render_template(
        "archive.html",
        posts=posts,
        stats=_archive_stats(posts),
        saved_posts=saved,
        platform_labels=PLATFORM_LABELS,
        categories=ALL_CATEGORIES,
        today=utcnow().astimezone(EASTERN).strftime("%A, %B %-d"),
        library_photos=_available_photos(),
        posts_with_photo={p.post_id for p in
                          session.query(Photo).filter_by(status="reserved")},
        card_posts={p.id for p in saved if _uses_card(p)},
    )


def _back():
    """Saved-post actions live on the Archive tab; everything else returns
    to the dashboard."""
    target = "archive" if request.form.get("return_to") == "archive" else "dashboard"
    return redirect(url_for(target))


def _get_post_or_404(post_id: str) -> Post:
    post = SessionLocal().get(Post, post_id)
    if post is None:
        abort(404)
    return post


def _post_text(caption, hashtags) -> str:
    return f"{caption}\n\nHashtags: {hashtags or '(none)'}"


def _uses_card(post) -> bool:
    """True when the post's image is its own rendered quote card (not a
    library photo)."""
    return post.image_url.split("?")[0].endswith(f"/{post.id}.jpg")


def _card_url(post) -> str:
    """The card's URL with a cache-buster, so a re-rendered card actually
    shows up instead of the browser's cached copy."""
    return (f"{config.APP_BASE_URL}/static/media/{post.id}.jpg"
            f"?v={int(utcnow().timestamp())}")


def _apply_card_edit(post) -> bool:
    """Dr. Ike rewrote the line on the quote card — re-render it. Only posts
    that use the card expose the field."""
    text = (request.form.get("image_text") or "").strip()
    if not text or text == post.image_text or not _uses_card(post):
        return False
    post.image_text = text if len(text) <= 140 else text[:137].rstrip() + "…"
    render_card(post.image_text, str(config.MEDIA_DIR / f"{post.id}.jpg"))
    post.image_url = _card_url(post)
    return True


def _apply_edits(session, post) -> str | None:
    """Apply form edits (caption, hashtags, card text). Returns the pre-edit
    text if the caption or hashtags changed — card-text tweaks re-render the
    image but don't feed the learning loop."""
    caption = (request.form.get("caption") or "").strip()
    if post.status not in ("pending", "saved") or not caption:
        return None
    hashtags = (request.form.get("hashtags") or "").strip()
    card_changed = _apply_card_edit(post)
    before = _post_text(post.caption, post.hashtags)
    text_changed = _post_text(caption, hashtags) != before
    if text_changed:
        post.caption = caption
        post.hashtags = hashtags
    if text_changed or card_changed:
        session.commit()
    return before if text_changed else None


@app.post("/post/<post_id>/save")
@login_required
def save(post_id):
    session = SessionLocal()
    post = _get_post_or_404(post_id)
    if post.status not in ("pending", "saved"):
        flash("That post was already actioned.", "error")
    else:
        before = _apply_edits(session, post)
        flash("Changes saved.", "ok")
        if before:
            _record_correction("edit", post, before,
                               after=_post_text(post.caption, post.hashtags))
    return _back()


@app.post("/post/<post_id>/save-later")
@login_required
def save_later(post_id):
    session = SessionLocal()
    post = _get_post_or_404(post_id)
    _apply_edits(session, post)  # keep any edits he made before setting it aside
    claimed = (
        session.query(Post)
        .filter_by(id=post.id, status="pending")
        .update({"status": "saved"})
    )
    session.commit()
    if claimed:
        flash("Saved for later — find it any time under the Archive tab, "
              "and a fresh option can take its slot here.", "ok")
    else:
        flash("That post was already actioned.", "error")
    return redirect(url_for("dashboard"))


@app.post("/post/<post_id>/discard")
@login_required
def discard(post_id):
    session = SessionLocal()
    post = _get_post_or_404(post_id)
    claimed = (
        session.query(Post)
        .filter_by(id=post.id, status="saved")
        .update({"status": "rejected"})
    )
    session.commit()
    if claimed:
        _release_photos(session, post)  # its photo goes back to the library
        flash("Removed from saved posts.", "ok")
    else:
        flash("That post was already actioned.", "error")
    return _back()


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

    avoid = [p.caption for p in _pending_posts()] + [post.caption]

    # custom drafts regenerate from their own source material
    if post.category in CUSTOM_CATEGORIES:
        filename = post.image_url.split("?")[0].rsplit("/", 1)[-1]
        image_filename = (filename if filename.startswith("custom-")
                          and (config.MEDIA_DIR / filename).exists() else None)
        source = post.source_theme or ""
        if note:
            source += f"\n\nDirection from Dr. Ike for the redo: {note}"
        try:
            _create_custom_post(post.category, source, image_filename,
                                avoid_captions=avoid,
                                post_group_id=post.post_group_id,
                                revision_of=post.id)
            flash("New custom option generated.", "ok")
        except Exception as e:
            app.logger.exception("Custom redo generation failed")
            flash(f"Couldn't generate a replacement: {e}", "error")
        return redirect(url_for("dashboard"))

    # regenerate within the same category; legacy rows fall back to an uncovered one
    category = post.category
    if category not in CATEGORIES:
        covered = {p.category for p in _pending_posts()}
        category = next((c for c in CATEGORIES if c not in covered), next(iter(CATEGORIES)))
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
    # if publishing fails, the card goes right back where it was
    prev_status = post.status if post.status in ("pending", "saved") else "pending"

    before = _apply_edits(session, post)
    if before:
        _record_correction("edit", post, before,
                           after=_post_text(post.caption, post.hashtags))

    # atomic claim: only one request can move pending/saved -> publishing
    claimed = (
        session.query(Post)
        .filter(Post.id == post.id, Post.status.in_(("pending", "saved")))
        .update({"status": "publishing", "approved_at": utcnow()},
                synchronize_session=False)
    )
    session.commit()
    if not claimed:
        flash("That post was already actioned.", "error")
        return _back()

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

    # failure returns the post to its old spot with the error on the card —
    # nothing ever disappears from the dashboard
    post.status = prev_status if error else "published"
    post.published_at = None if error else utcnow()
    post.error = f"{label}: {error}" if error else None
    session.commit()
    if error:
        flash(f"Publishing to {label} failed — the post is back below with the "
              f"error on it: {error[:200]}", "error")
    else:
        _retire_photos(session, post)   # a published photo is used exactly once
        flash(f"Published to {label}.", "ok")
    return _back()


@app.post("/post/<post_id>/photo")
@login_required
def set_photo(post_id):
    """Attach a library photo to a draft (chosen or auto-matched), swap it,
    or remove it and fall back to the branded quote card."""
    session = SessionLocal()
    post = _get_post_or_404(post_id)
    if post.status not in ("pending", "saved"):
        flash("That post was already actioned.", "error")
        return _back()
    _apply_edits(session, post)  # keep any caption edits made before clicking

    if request.form.get("remove"):
        _release_photos(session, post)
        path = config.MEDIA_DIR / f"{post.id}.jpg"
        if not path.exists():  # post was born with a photo — render its card now
            render_card(post.image_text, str(path))
        post.image_url = _card_url(post)
        session.commit()
        flash("Photo removed — this post uses the branded quote card.", "ok")
        return _back()

    if request.form.get("photo_id"):
        photo = session.get(Photo, request.form["photo_id"])
        if photo is None or photo.status != "available":
            flash("That photo isn't available anymore.", "error")
            return _back()
    else:  # auto-match the best-fitting photo
        photos = _available_photos()
        if not photos:
            flash("No photos in the library yet — add some first.", "error")
            return _back()
        try:
            choice = pick_photo(post.caption,
                                [(i, p.description) for i, p in enumerate(photos)])
        except Exception as e:
            app.logger.exception("Photo auto-match failed for %s", post.id)
            flash(f"Auto-match failed: {e}", "error")
            return _back()
        photo = photos[choice] if choice is not None and 0 <= choice < len(photos) else photos[0]

    _release_photos(session, post)  # swap: any current photo returns to the library
    photo.status = "reserved"
    photo.post_id = post.id
    post.image_url = f"{config.APP_BASE_URL}/static/media/{photo.filename}"
    session.commit()
    flash("Photo attached to this post.", "ok")
    return _back()


# ---------------------------------------------------------------- analytics

@app.post("/publication/<pub_id>/metrics")
@login_required
def set_metrics(pub_id):
    """LinkedIn shows analytics only on the post itself — Dr. Ike copies the
    numbers in here so the learning loop gets LinkedIn data too."""
    session = SessionLocal()
    pub = session.get(Publication, pub_id)
    if pub is None or pub.status != "published":
        abort(404)
    updated = False
    for key in ("impressions", "likes", "comments", "shares"):
        raw = (request.form.get(key) or "").strip().replace(",", "")
        if raw:
            try:
                setattr(pub, key, max(0, int(raw)))
                updated = True
            except ValueError:
                pass
    if updated:
        pub.metrics_source = "manual"
        pub.metrics_updated_at = utcnow()
        session.commit()
        flash("Numbers saved — what performs well steers what gets written next.", "ok")
    else:
        flash("Enter at least one number first.", "error")
    return redirect(url_for("archive"))


# ---------------------------------------------------------------- lessons

@app.post("/lessons/add")
@login_required
def add_lesson():
    """Dr. Ike (or Vernon) writes a rule directly — it steers every new post,
    same as the distilled ones."""
    text = (request.form.get("text") or "").strip()
    if not text:
        flash("Write the rule first.", "error")
    else:
        session = SessionLocal()
        session.add(Lesson(category=None, text=text[:400], source="manual"))
        session.commit()
        flash("Rule added — every new post will follow it.", "ok")
    return redirect(url_for("dashboard"))


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


if __name__ == "__main__":
    app.run(debug=True)
