"""Per-post analytics from Instagram.

Fetched metrics feed the performance-steering loop (top-scoring posts are
injected into future generation prompts) and are shown per post in the
Archive tab.
- Instagram: fetched live from the Graph API.
- LinkedIn: the API exposes no post analytics for personal profiles, so
  LinkedIn publications carry no metrics.
- USE_MOCK_PUBLISHER=true: deterministic mock numbers so the learning loop
  can be exercised in dev.
"""
import hashlib
import logging

import requests

import config
from db import SessionLocal
from models import IgCredentials, utcnow

log = logging.getLogger(__name__)

STALE_AFTER_HOURS = 12


# ---------------------------------------------------------------- helpers

def _ig_token():
    creds = SessionLocal().get(IgCredentials, 1)
    return (creds.access_token, creds.ig_user_id) if creds else (None, None)


def _mock_metrics(pub) -> dict:
    """Deterministic per-publication numbers so dev refreshes don't jitter."""
    seed = int(hashlib.md5(pub.id.encode()).hexdigest()[:7], 16)
    impressions = 900 + seed % 4600
    likes = int(impressions * (0.03 + (seed % 50) / 1000))
    return {
        "impressions": impressions,
        "likes": likes,
        "comments": max(0, likes // (4 + seed % 6)),
        "shares": max(0, likes // (7 + seed % 9)),
    }


# ---------------------------------------------------------------- per-post

def _instagram_metrics(pub) -> dict | None:
    token, _ = _ig_token()
    if not token or not pub.external_id:
        return None
    metrics = {}
    resp = requests.get(
        f"{config.GRAPH_API_BASE}/{pub.external_id}",
        params={"fields": "like_count,comments_count", "access_token": token},
        timeout=30,
    )
    if resp.status_code == 200:
        data = resp.json()
        metrics["likes"] = data.get("like_count")
        metrics["comments"] = data.get("comments_count")
    else:
        log.warning("IG media fields failed (%s): %s", resp.status_code, resp.text)

    # reach/saves come from the insights edge; tolerate partial failures
    resp = requests.get(
        f"{config.GRAPH_API_BASE}/{pub.external_id}/insights",
        params={"metric": "reach,saved,shares", "access_token": token},
        timeout=30,
    )
    if resp.status_code == 200:
        for item in resp.json().get("data", []):
            values = item.get("values") or [{}]
            value = values[0].get("value")
            if item.get("name") == "reach":
                metrics["impressions"] = value
            elif item.get("name") in ("saved", "shares"):
                metrics["shares"] = (metrics.get("shares") or 0) + (value or 0)
    else:
        log.warning("IG insights failed (%s): %s", resp.status_code, resp.text)
    return metrics or None


def refresh_publication(session, pub) -> bool:
    """Fetch fresh metrics for one publication. Returns True if updated."""
    if pub.status != "published":
        return False
    if config.USE_MOCK_PUBLISHER:
        metrics = _mock_metrics(pub)
    elif pub.platform == "instagram":
        metrics = _instagram_metrics(pub)
    else:
        return False  # linkedin: manual entry only (no personal-profile API)

    if not metrics:
        return False
    for key in ("impressions", "likes", "comments", "shares"):
        if metrics.get(key) is not None:
            setattr(pub, key, metrics[key])
    pub.metrics_source = "mock" if config.USE_MOCK_PUBLISHER else "api"
    pub.metrics_updated_at = utcnow()
    session.commit()
    return True


def is_stale(pub) -> bool:
    if pub.platform == "linkedin" and not config.USE_MOCK_PUBLISHER:
        return False
    if pub.metrics_updated_at is None:
        return True
    updated = pub.metrics_updated_at
    age_hours = (utcnow().timestamp()
                 - updated.replace(tzinfo=utcnow().tzinfo).timestamp()) / 3600
    return age_hours > STALE_AFTER_HOURS
