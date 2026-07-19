"""Claude call -> {caption, hashtags, image_text}.

Each daily batch has six posts: three for Instagram (short, casual,
quote-forward) and three for LinkedIn (his long-form professional voice).
The LinkedIn "article" category gets web search so it can react to a real
current article; everything else is drawn from Dr. Ike's experience.
"""
import json
import logging
import re

import anthropic

import config

log = logging.getLogger(__name__)

# Ordered: one dashboard option per category. Keys are stored on posts, so
# renaming a key orphans old rows — add new keys instead.
CATEGORIES = {
    # ---- Instagram: short, casual, quote-on-the-card ----
    "ig_career": {
        "platform": "instagram",
        "label": "Career Motivation",
        "brief": (
            "career motivation — a short, punchy encouragement about careers:"
            " getting unstuck, being overlooked while less capable people move"
            " up, competency vs. influence, betting on yourself. Draw on his"
            " Triangle of Leadership ideas (competency attracts, influence"
            " moves, leverage scales) without lecturing."
        ),
        "web_search": False,
    },
    "ig_medspace": {
        "platform": "instagram",
        "label": "Medspace Motivation",
        "brief": (
            "motivation about the medical space — an inspirational take on the"
            " progress happening in medicine and life sciences: breakthroughs"
            " becoming real for patients, how far treatment has come, why the"
            " work matters. Uplifting and big-picture; no clinical claims, no"
            " numbers that would need a citation, nothing that reads as"
            " medical advice."
        ),
        "web_search": False,
    },
    "ig_motivation": {
        "platform": "instagram",
        "label": "Motivation",
        "brief": (
            "general motivation — discipline, resilience, self-belief, growth,"
            " showing up on the hard days. Universal, warm, and quotable;"
            " something anyone scrolling could screenshot and keep."
        ),
        "web_search": False,
    },
    # ---- LinkedIn: his long-form professional voice ----
    "li_article": {
        "platform": "linkedin",
        "label": "Article Response",
        "brief": (
            "a response to one genuinely recent, credible article — search the"
            " web and pick the single most compelling piece of the day from"
            " EITHER leadership/career-development OR the biopharma/medical"
            " space (major journal, regulator, congress, or reputable industry"
            " or business press). Write HIS take in his own words: what the"
            " article gets right or misses, what it means for the reader's"
            " career or for industry strategy. Name the source naturally in"
            " the caption and include the article's URL on its own line at the"
            " end of the caption. Do not invent articles or findings; if"
            " search yields nothing solid, write a grounded perspective piece"
            " on a live industry question and skip the link."
        ),
        "web_search": True,
    },
    "li_career": {
        "platform": "linkedin",
        "label": "Career Motivation",
        "brief": (
            "career motivation in his own words — practical, encouraging"
            " career insight for professionals in medicine, science, or"
            " biopharma: why competency alone doesn't get you promoted,"
            " influence and leverage, hiring character over resume, mentorship,"
            " pivots from clinic to industry. Speak from his 17+ years in"
            " pharma; concrete over platitudes, drawn from lived experience."
        ),
        "web_search": False,
    },
    "li_quote": {
        "platform": "linkedin",
        "label": "Motivational Quote",
        "brief": (
            "a motivational quote or short motivational paragraph in LinkedIn"
            " format — open with the quote or the one-line idea (his own line,"
            " or one of his book's lines), then unpack it in a few short"
            " standalone lines with white space between them, and close with a"
            " question that invites comments. Keep it under 120 words."
        ),
        "web_search": False,
    },
}

WHO_HE_IS = """WHO HE IS:
- Physician and biopharma executive (17+ years in pharma, 20+ in medicine):
  former CMO & Head of Medical Affairs; advisor to biopharma founders,
  investors, and medical affairs leaders; $5B+ in exits. Fractional CEO/CMO
  for preclinical-to-Phase-2 biotechs.
- Author of "Dr. Ike's Triangle of Leadership: How to Attract, Move, and
  Scale People" — "the little book with big ideas". The framework: Competency
  attracts people. Influence moves people. Leverage scales people. Signature
  line: "Leadership is not about authority. It's about the ability to
  attract followers."
- Signature framing: competency gets you into the room, but influence and
  leverage move you forward; leadership is not about titles, it's about
  creating enough value that people willingly choose to follow.
- Therapeutic areas he actually covers: obesity/cardiometabolic (GLP-1s,
  amylin, MASH), nephrology and kidney health, rare and specialty disease,
  immunology, diabetes; plus AI in pharma and clinical-trial diversity."""

HUMAN_RULES = """WRITE LIKE A HUMAN:
- NEVER use em dashes (—), double hyphens (--), or a hyphen used as a pause.
  Use a comma, a period, or a new line instead. This is a hard rule.
- No corporate filler ("delve", "landscape", "game-changer", "in today's
  fast-paced world"). Plain words, short sentences.
- ALWAYS include 2 to 5 hashtags (they drive viewership): mix one or two
  high-reach tags with niche tags specific to the post's topic. Hashtags go
  in the hashtags field, not the caption."""

VOICE_LINKEDIN = """VOICE FOR THIS LINKEDIN POST (from his real posts):
- Hook first: open with the tension or the headline line, then unpack it.
- LinkedIn format: short standalone lines separated by blank lines, not
  dense paragraphs. A tight bullet list (using the character •) is welcome
  when it fits. Close with a question that invites comments.
- Data-precise and skeptical-but-fair when facts are involved: exact numbers
  with caveats, "signal worth tracking, not yet a story to tell".
- Measured, credible, warm. Never hypey or clickbait. Nothing that reads as
  medical advice or an efficacy claim about a product.
- Almost no emoji.

MATCH HIS STYLE using these real posts of his as reference for length,
structure, and tone:
{samples}"""

VOICE_INSTAGRAM = """VOICE FOR THIS INSTAGRAM POST:
- You are writing for Instagram, not LinkedIn. The branded image card carries
  the quote (the image_text field); the caption is the short, casual voice
  under it.
- Caption: 1 to 4 short lines, conversational and direct ("you"), warm and
  human. One or two emojis are fine where they feel natural, never more.
- Quotable over clever: image_text must be a short, punchy line (under 120
  characters) that stands alone on a quote card. His own words or his book's
  lines are perfect; never invent a quote attributed to someone else.
- A soft call to action is welcome sometimes (save this, send it to someone
  who needs it, drop a comment) but don't force one into every post.
- Still him: grounded and encouraging, a doctor and executive who has lived
  it. Casual never means sloppy or hypey.

HIS SIGNATURE LINES AND IDEAS (draw on these, don't copy them every time):
{samples}"""

PROMPT_TEMPLATE = """You are the social media writer for Iroko Lifesciences Advisory, the biopharma
strategic advisory practice of Dr. Ike Ogbaa, MD. You write his social posts
in his established voice. This post is for {platform_label}.

{who}

{human_rules}

{voice}

{task}

Your FINAL message must be ONLY valid JSON, no preamble, no markdown fences:
{{"caption":"...", "hashtags":"2 to 5 hashtags separated by spaces", "image_text":"a quotable line <=140 chars", "photo": <library photo number, or null>}}"""

REQUIRED_KEYS = ("caption", "hashtags", "image_text")

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}


def _read_text(path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _humanize(text: str) -> str:
    """Dr. Ike wants no em dashes or double hyphens anywhere in the copy."""
    text = re.sub(r"\s*(?:--+|—|–)\s+", ", ", text)   # dash used as a pause
    text = re.sub(r"(?<=\w)(?:--+|—)(?=\w)", ", ", text)  # word—word
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return re.sub(r",{2,}", ",", text)


def _voice_block(category: str) -> str:
    if CATEGORIES[category]["platform"] == "instagram":
        samples = _read_text(config.BOOK_QUOTES_FILE) or "(none on file)"
        return VOICE_INSTAGRAM.format(samples=samples)
    samples = "\n\n".join(
        s for s in (_read_text(config.SAMPLE_POSTS_FILE),
                    _read_text(config.BOOK_QUOTES_FILE)) if s
    ) or "(none on file)"
    return VOICE_LINKEDIN.format(samples=samples)


def _build_prompt(category: str, source_theme: str | None,
                  avoid_captions: list[str] | None,
                  lessons: list[str] | None = None,
                  top_performers: list[tuple[int, str]] | None = None,
                  photos: list[tuple[int, str]] | None = None,
                  campaign: str | None = None) -> str:
    spec = CATEGORIES[category]
    task = f"TASK: Write ONE post. Today's category: {spec['brief']}"
    if campaign and campaign.strip():
        task += (
            "\n\nACTIVE CAMPAIGN — Dr. Ike has set a posting focus for this"
            " period, and every post should serve it while staying true to its"
            " category:\n" + campaign.strip() +
            "\nWork the campaign in naturally (an angle, a mention, or a soft"
            " closing call to action). Never let the post read like an ad, and"
            " never sacrifice the category's voice for the pitch."
        )
    if photos:
        lines = "\n".join(f"{i}. {desc}" for i, desc in photos)
        task += (
            "\n\nPHOTO LIBRARY — Dr. Ike's available photos:\n" + lines +
            "\n\nIf one of these photos GENUINELY fits the post you're writing,"
            " set \"photo\" to its number and reference the moment naturally in"
            " the caption. If none fits, set \"photo\" to null and a branded"
            " quote card will be used instead. Never force a fit."
        )
    else:
        task += "\n\n(No photos available — set \"photo\" to null.)"
    if source_theme and source_theme.strip():
        task += f"\n\nSpecific direction from Dr. Ike for this post: {source_theme.strip()}"
    if lessons:
        rules = "\n".join(f"- {l.strip()}" for l in lessons if l and l.strip())
        if rules:
            task += (
                "\n\nLESSONS FROM DR. IKE'S PAST CORRECTIONS — follow these"
                " strictly; he edited earlier drafts to teach them:\n" + rules
            )
    if top_performers:
        hits = "\n".join(
            f"- (engagement score {score}) {caption[:180]}"
            for score, caption in top_performers if caption
        )
        if hits:
            task += (
                "\n\nWHAT HAS PERFORMED BEST — these recent posts earned the most"
                " engagement. Favor similar topics, angles, and hook styles"
                " WITHOUT repeating their content:\n" + hits
            )
    if avoid_captions:
        drafts = "\n".join(f"- {c[:200]}" for c in avoid_captions if c and c.strip())
        if drafts:
            task += (
                "\n\nToday's other drafts are below. Write something clearly different"
                " from all of them — different topic, angle, and opening line:\n" + drafts
            )
    return PROMPT_TEMPLATE.format(
        platform_label="Instagram" if spec["platform"] == "instagram" else "LinkedIn",
        who=WHO_HE_IS,
        human_rules=HUMAN_RULES,
        voice=_voice_block(category),
        task=task,
    )


def _extract_json(text: str) -> dict:
    """Strip ```json fences if present, locate the JSON object, validate keys."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    # with web search enabled the model may write a sentence before the JSON —
    # fall back to the outermost braces
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(cleaned[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    missing = [k for k in ("caption", "image_text") if not str(data.get(k, "")).strip()]
    if missing:
        raise ValueError(f"missing/empty keys in response: {missing}")
    result = {k: str(data.get(k, "")).strip() for k in REQUIRED_KEYS}
    result["caption"] = _humanize(result["caption"])
    result["image_text"] = _humanize(result["image_text"])
    # 2-5 hashtags on every post — they drive viewership
    tags = [t for t in result["hashtags"].split() if t.startswith("#")]
    if len(tags) < 2:
        raise ValueError(f"expected 2-5 hashtags, got {len(tags)}")
    result["hashtags"] = " ".join(tags[:5])
    if len(result["image_text"]) > 140:
        result["image_text"] = result["image_text"][:137].rstrip() + "…"
    photo = data.get("photo")
    result["photo"] = photo if isinstance(photo, int) and not isinstance(photo, bool) else None
    return result


def _call_claude(client, content, use_search: bool):
    """content: a prompt string, or a content-block list (e.g. image + text)."""
    kwargs = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": 4000 if use_search else 2000,
        "messages": [{"role": "user", "content": content}],
    }
    if use_search:
        kwargs["tools"] = [WEB_SEARCH_TOOL]

    response = client.messages.create(**kwargs)
    # server-side web search can pause a long turn; re-send to let it resume
    for _ in range(3):
        if response.stop_reason != "pause_turn":
            break
        kwargs["messages"] = [
            {"role": "user", "content": content},
            {"role": "assistant", "content": response.content},
        ]
        response = client.messages.create(**kwargs)

    text_blocks = [b.text for b in response.content if b.type == "text"]
    if not text_blocks:
        raise ValueError("no text in Claude response")
    return text_blocks[-1]  # with search, earlier blocks may be commentary


def generate_post(category: str, source_theme: str | None = None,
                  avoid_captions: list[str] | None = None,
                  lessons: list[str] | None = None,
                  top_performers: list[tuple[int, str]] | None = None,
                  photos: list[tuple[int, str]] | None = None,
                  campaign: str | None = None) -> dict:
    """Returns {caption, hashtags, image_text, photo}. Raises RuntimeError on failure.

    category: key from CATEGORIES.
    source_theme: optional steering note from Dr. Ike (redo notes, cron themes).
    avoid_captions: captions of the other drafts in today's batch.
    lessons: durable style rules distilled from his past corrections.
    top_performers: [(engagement_score, caption)] of his best-performing posts.
    photos: [(index, description)] of available library photos; the returned
        "photo" is the chosen index, or None to use the branded quote card.
    campaign: instruction of the active posting focus, if one is running.
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category}")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
    prompt = _build_prompt(category, source_theme, avoid_captions, lessons,
                           top_performers, photos, campaign)
    use_search = CATEGORIES[category]["web_search"]
    last_err = None
    for attempt in (1, 2):
        text = _call_claude(client, prompt, use_search)
        try:
            return _extract_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            log.warning("attempt %d: Claude response was not valid JSON: %s", attempt, e)
    raise RuntimeError(f"Claude did not return valid post JSON after 2 attempts: {last_err}")


def describe_photo(image_path: str) -> str:
    """One vision call at upload time: a short description used to match the
    photo to future posts."""
    import base64
    from pathlib import Path

    image_data = base64.standard_b64encode(Path(image_path).read_bytes()).decode()
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
    response = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg",
                            "data": image_data}},
                {"type": "text", "text":
                 "Describe this photo in 1-2 sentences for a content library:"
                 " who/what is shown, the setting, and the mood. Plain text only."},
            ],
        }],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise ValueError("empty photo description")
    return text[:500]
