"""Publisher interface: one publisher per platform, plus a mock for dev.

get_publisher("instagram" | "linkedin") returns the right one;
USE_MOCK_PUBLISHER=true swaps in a mock for every platform.
"""
import logging
import uuid

import requests

import config
from db import SessionLocal
from models import IgCredentials

log = logging.getLogger(__name__)

# "instagram" covers Instagram & Facebook (Meta crossposting shares the post
# to the connected Facebook Page once the Page credentials are wired).
PLATFORMS = ("instagram", "linkedin", "x")
PLATFORM_LABELS = {"instagram": "Instagram & Facebook", "linkedin": "LinkedIn",
                   "x": "X"}


def full_caption(post) -> str:
    caption = post.caption
    if post.hashtags:
        caption += "\n\n" + post.hashtags
    return caption


class Publisher:
    platform = ""

    def publish(self, post) -> str:
        """Publish the post; return the platform's post/media id or raise."""
        raise NotImplementedError


class MockPublisher(Publisher):
    def __init__(self, platform: str):
        self.platform = platform

    def publish(self, post) -> str:
        media_id = f"mock_{self.platform}_{uuid.uuid4().hex}"
        log.info("MockPublisher(%s): pretending to publish post %s -> %s",
                 self.platform, post.id, media_id)
        return media_id


class InstagramPublisher(Publisher):
    """Two-step Instagram Graph API publish: create container, then publish it."""

    platform = "instagram"

    def publish(self, post) -> str:
        creds = SessionLocal().get(IgCredentials, 1)
        if creds is None:
            raise RuntimeError(
                "ig_credentials is empty — set IG_USER_ID and IG_ACCESS_TOKEN and restart"
            )

        # 1. Create media container
        resp = requests.post(
            f"{config.GRAPH_API_BASE}/{creds.ig_user_id}/media",
            data={
                "image_url": post.image_url,
                "caption": full_caption(post),
                "access_token": creds.access_token,
            },
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"IG container creation failed ({resp.status_code}): {resp.text}")
        creation_id = resp.json()["id"]

        # 2. Publish the container
        resp = requests.post(
            f"{config.GRAPH_API_BASE}/{creds.ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": creds.access_token},
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"IG media publish failed ({resp.status_code}): {resp.text}")
        return resp.json()["id"]


# LinkedIn's Posts API requires these characters to be escaped in commentary text
_LINKEDIN_RESERVED = "\\|{}@[]()<>*_~"


def _linkedin_escape(text: str) -> str:
    for ch in _LINKEDIN_RESERVED:
        text = text.replace(ch, "\\" + ch)
    return text


class LinkedInPublisher(Publisher):
    """Three-step LinkedIn publish: initialize image upload, upload bytes, create post."""

    platform = "linkedin"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {config.LINKEDIN_ACCESS_TOKEN}",
            "LinkedIn-Version": config.LINKEDIN_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    def _image_bytes(self, post) -> bytes:
        # the rendered card lives on this server's disk — prefer the local file
        # (image_url may carry a ?v= cache-buster; strip it before the path lookup)
        filename = post.image_url.split("?")[0].rsplit("/", 1)[-1]
        path = config.MEDIA_DIR / filename
        if path.exists():
            return path.read_bytes()
        resp = requests.get(post.image_url, timeout=60)
        resp.raise_for_status()
        return resp.content

    def publish(self, post) -> str:
        if not (config.LINKEDIN_ACCESS_TOKEN and config.LINKEDIN_AUTHOR_URN):
            raise RuntimeError("LINKEDIN_ACCESS_TOKEN / LINKEDIN_AUTHOR_URN are not set")

        # 1. Initialize the image upload
        resp = requests.post(
            f"{config.LINKEDIN_API_BASE}/images?action=initializeUpload",
            json={"initializeUploadRequest": {"owner": config.LINKEDIN_AUTHOR_URN}},
            headers=self._headers(),
            timeout=60,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"LinkedIn upload init failed ({resp.status_code}): {resp.text}")
        value = resp.json()["value"]
        upload_url, image_urn = value["uploadUrl"], value["image"]

        # 2. Upload the image bytes
        resp = requests.put(
            upload_url,
            data=self._image_bytes(post),
            headers={
                "Authorization": f"Bearer {config.LINKEDIN_ACCESS_TOKEN}",
                "Content-Type": "application/octet-stream",
            },
            timeout=120,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"LinkedIn image upload failed ({resp.status_code}): {resp.text}")

        # 3. Create the post
        resp = requests.post(
            f"{config.LINKEDIN_API_BASE}/posts",
            json={
                "author": config.LINKEDIN_AUTHOR_URN,
                "commentary": _linkedin_escape(full_caption(post)),
                "visibility": "PUBLIC",
                "distribution": {
                    "feedDistribution": "MAIN_FEED",
                    "targetEntities": [],
                    "thirdPartyDistributionChannels": [],
                },
                "content": {"media": {"id": image_urn, "altText": post.image_text[:300]}},
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            },
            headers=self._headers(),
            timeout=60,
        )
        if resp.status_code != 201:
            raise RuntimeError(f"LinkedIn post creation failed ({resp.status_code}): {resp.text}")
        return resp.headers.get("x-restli-id", "")


class XPublisher(Publisher):
    """X's API isn't wired yet (needs a paid API tier + OAuth keys). Until
    then X posts stay manual: copy the caption, post it with the image."""

    platform = "x"

    def publish(self, post) -> str:
        raise RuntimeError(
            "X publishing isn't connected yet — copy the caption and post it "
            "on X by hand, or keep this option in test mode."
        )


_REAL_PUBLISHERS = {
    "instagram": InstagramPublisher,
    "linkedin": LinkedInPublisher,
    "x": XPublisher,
}


def get_publisher(platform: str) -> Publisher:
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform: {platform}")
    if config.USE_MOCK_PUBLISHER:
        return MockPublisher(platform)
    return _REAL_PUBLISHERS[platform]()
