# Ike Social Agent

A social posting agent for **Dr. Ike Ogbaa, MD** (Iroko Lifesciences Advisory).

The flow:

1. A daily cron (or the dashboard button) generates **three post options, one per
   category** — Medical space (with web search, so it reacts to actual current
   developments), Personal growth, and Career motivation. Each is an on-brand
   caption + hashtags + quotable line written by Claude, rendered as a branded
   1080×1080 quote card with Pillow, and saved as a **pending** draft. Each option
   is told about the others so the three don't overlap. Categories are defined in
   `generator.CATEGORIES` — edit the briefs there to steer the content mix.
2. Dr. Ike **logs in to the live dashboard** (password-protected) and reviews the options.
3. For each option he can **Approve & publish**, **Save changes** (edit caption/hashtags),
   or **Redo** (discard and regenerate, optionally with a steering note).
4. Approving publishes to the platforms he selects — **Instagram**, **Facebook**,
   and/or **LinkedIn** — with a per-platform result recorded in the `publications` table.

## Project layout

```
app.py                Flask app + routes (login, dashboard, generate, publish)
config.py             loads env vars
models.py             SQLAlchemy models (posts, publications, ig_credentials)
db.py                 engine/session setup + init_db()
generator.py          Claude call -> caption/hashtags/image_text
imagegen.py           Pillow -> branded JPEG quote card
publisher.py          Instagram / Facebook / LinkedIn publishers + dev mock
refresh_token.py      standalone script for the Render Cron Job (IG token)
templates/            login.html, dashboard.html
static/brand.css      design tokens extracted from drikeadvisory.com
static/fonts/         Fraunces + Inter TTFs (bundled so cards render anywhere)
static/media/         rendered post images (served publicly)
```

## Run locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill in values; keep USE_MOCK_PUBLISHER=true
flask --app app run
```

With no `DATABASE_URL` set, a local SQLite file (`dev.db`) is used.

Open `http://localhost:5000/`, sign in with `DASHBOARD_PASSWORD`, and click
**Generate today's options**. With `USE_MOCK_PUBLISHER=true`, publishing logs a fake
media id per platform instead of hitting the real APIs.

The cron endpoint still exists for scheduled generation (tops the dashboard up to
`DAILY_POST_COUNT` pending options):

```bash
curl -s -X POST http://localhost:5000/generate -H "X-Generate-Secret: <secret>"
```

### Sample posts (voice matching)

`sample_posts.txt` holds transcriptions of Dr. Ike's real LinkedIn posts and is
injected into the Claude prompt as style reference. Add new posts to it over time
(plain text, any separator) to keep the voice current — it's checked into the repo
so it deploys with the app.

## Environment variables

See `.env.example`. Notes:

| Variable | Notes |
|---|---|
| `ANTHROPIC_MODEL` | defaults to `claude-sonnet-5` |
| `DASHBOARD_PASSWORD` | required — the dashboard refuses logins until it's set |
| `GENERATE_SECRET` | **set this in production** — without it, `POST /generate` is open |
| `APP_BASE_URL` | must be the public HTTPS URL; Instagram/Facebook fetch images from it |
| `USE_MOCK_PUBLISHER` | `true` in dev; `false` to publish for real (all platforms) |
| `IG_USER_ID` / `IG_ACCESS_TOKEN` | seed the `ig_credentials` table on first startup |
| `FB_PAGE_ID` / `FB_PAGE_ACCESS_TOKEN` | Facebook Page publishing (same Meta app) |
| `LINKEDIN_ACCESS_TOKEN` / `LINKEDIN_AUTHOR_URN` | LinkedIn publishing (see below) |
| `FB_APP_ID` / `FB_APP_SECRET` | used only by `refresh_token.py` (fb_exchange_token grant) |

## Deploy to Render

The repo ships a **Blueprint** (`render.yaml`) that creates the whole stack:
dashboard.render.com → **New +** → **Blueprint** → pick this repo. It provisions:

1. **Postgres** (`ike-agent-db`) — wired into `DATABASE_URL` automatically.
2. **Web Service** (`ike-agent`) — `gunicorn --timeout 600 --threads 8 app:app` (generation
   happens inside web requests and takes ~2 min for a full batch), with a 1 GB
   persistent disk mounted at `static/media`. You're prompted for
   `ANTHROPIC_API_KEY` and `DASHBOARD_PASSWORD`; `FLASK_SECRET_KEY` and
   `GENERATE_SECRET` are auto-generated. `USE_MOCK_PUBLISHER` starts as `true` —
   flip it to `false` in the dashboard once real platform credentials are in.
3. **Cron Job (daily options)** (`ike-agent-daily`, 11:00 UTC) — curls the web
   service's `POST /generate` with the `X-Generate-Secret` header. It must go
   through the web service (not run the generator itself) so rendered images
   land on the web service's disk. Options left unreviewed stay on the dashboard;
   the cron only fills categories that have no pending option.

When going live, add a **weekly token-refresh cron** — `python refresh_token.py`
with `DATABASE_URL`, `FB_APP_ID`, `FB_APP_SECRET` — to exchange the long-lived IG
token before it expires (~60 days).

## Platform requirements

### Instagram
- The image URL must be publicly reachable over **HTTPS**, a **JPEG**, aspect ratio
  4:5–1.91:1 (cards are 1:1, which is inside the range). Rendered cards are served from
  `{APP_BASE_URL}/static/media/<uuid>.jpg`.
- Publishing uses `graph.facebook.com/v21.0`: `POST /{ig_user_id}/media` (container) then
  `POST /{ig_user_id}/media_publish`.

### Facebook
- Publishes the card as a **Page photo post**: `POST /{page_id}/photos` with `url` +
  `caption`. Needs a Page access token with `pages_manage_posts` from the same Meta app.

### LinkedIn
- Three-step publish: `POST /rest/images?action=initializeUpload` → `PUT` the image
  bytes → `POST /rest/posts`. Needs an OAuth token with `w_member_social` (personal
  profile) or `w_organization_social` (company page), and the matching
  `LINKEDIN_AUTHOR_URN` (`urn:li:person:…` or `urn:li:organization:…`).
- LinkedIn tokens expire after ~60 days and there's no long-lived refresh outside
  their programmatic refresh program — expect to re-authorize periodically.

### Images on disk
Rendered images live on the service's local disk. On Render, attach a persistent disk
to the web service (mount at `static/media`) or images disappear on deploys/restarts —
fine for pending drafts published quickly, but a disk is safer.

## Post lifecycle

`pending` → (approve) → `publishing` → `published` | `failed`
`pending` → (redo) → `rejected`, and a new `pending` row is created with `revision_of`
pointing at the old draft and the same `post_group_id`.

Each publish attempt writes a row in `publications` (`post_id`, `platform`, `status`,
`external_id`, `error`). A post is `published` if **any** selected platform succeeded;
per-platform failures are reported in the dashboard's flash banner.

Approval is idempotent: the pending → publishing transition is an atomic claim, so a
double-click can never publish twice.
