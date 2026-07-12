"""Standalone script for a Render Cron Job (run weekly).

Refreshes the long-lived Instagram Graph API access token by exchanging the
current token via the fb_exchange_token grant (requires FB_APP_ID and
FB_APP_SECRET). Prevents publishing from dying ~60 days after launch.
"""
import logging
import sys
from datetime import timedelta

import requests

import config
from db import SessionLocal, init_db
from models import IgCredentials, utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("refresh_token")


def main() -> None:
    init_db()
    session = SessionLocal()
    creds = session.get(IgCredentials, 1)
    if creds is None:
        log.error("ig_credentials is empty — nothing to refresh. "
                  "Set IG_USER_ID / IG_ACCESS_TOKEN and start the app once to seed it.")
        sys.exit(1)
    if not config.FB_APP_ID or not config.FB_APP_SECRET:
        log.error("FB_APP_ID and FB_APP_SECRET must be set to refresh the token.")
        sys.exit(1)

    log.info("Refreshing IG access token (current expiry: %s)", creds.token_expires_at)
    resp = requests.get(
        f"{config.GRAPH_API_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": config.FB_APP_ID,
            "client_secret": config.FB_APP_SECRET,
            "fb_exchange_token": creds.access_token,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        log.error("Token refresh failed (%s): %s", resp.status_code, resp.text)
        sys.exit(1)

    data = resp.json()
    creds.access_token = data["access_token"]
    expires_in = data.get("expires_in")  # absent for never-expiring tokens
    creds.token_expires_at = utcnow() + timedelta(
        seconds=expires_in if expires_in else 60 * 24 * 3600
    )
    session.commit()
    log.info("Token refreshed OK. New expiry: %s", creds.token_expires_at)


if __name__ == "__main__":
    main()
