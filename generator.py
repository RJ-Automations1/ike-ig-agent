"""Claude call -> {caption, hashtags, image_text}.

Each daily batch has one post per category. The "medical" category gets web
search so it can react to actual current developments; the other two are
drawn from Dr. Ike's experience and don't need it.
"""
import json
import logging

import anthropic

import config

log = logging.getLogger(__name__)

# Ordered: one dashboard option per category, in this order.
CATEGORIES = {
    "medical": {
        "label": "Medical space",
        "brief": (
            "a current update in the medical/biopharma space that Dr. Ike is well"
            " placed to comment on — a data readout, congress signal, deal, or"
            " policy shift in his areas (obesity/cardiometabolic, nephrology, rare"
            " disease, immunology, AI in pharma, trial diversity/access). Use web"
            " search to find one genuinely recent, credible development (major"
            " journal, regulator, congress, or reputable industry press), then"
            " write HIS analytical take: exact numbers with caveats, and what it"
            " means for strategy — 'turn signal into strategy', not news"
            " reporting. Do not invent findings; if search yields nothing solid,"
            " write a grounded perspective piece on a live industry question."
        ),
        "web_search": True,
    },
    "growth": {
        "label": "Personal growth",
        "brief": (
            "personal growth — an honest, specific reflection on character,"
            " discipline, humility, resilience, or self-awareness, drawn from his"
            " lived experience as a physician and biopharma executive and echoing"
            " the themes of his Triangle of Leadership. Grounded and warm, never"
            " generic self-help."
        ),
        "web_search": False,
    },
    "career": {
        "label": "Career motivation",
        "brief": (
            "career motivation — practical, encouraging career insight for"
            " professionals in medicine, science, or biopharma: leadership,"
            " hiring and building teams (character over résumé), mentorship,"
            " career pivots from clinic to industry, how leaders treat people"
            " after they stop being useful. Speak from his 20+ years of"
            " experience; concrete over platitudes."
        ),
        "web_search": False,
    },
}

PROMPT_TEMPLATE = """You are the social media writer for Iroko Lifesciences Advisory, the biopharma
strategic advisory practice of Dr. Ike Ogbaa, MD. You write his social posts
(published to Instagram, Facebook, and LinkedIn) in his established voice.

WHO HE IS:
- Physician and biopharma executive (20+ years): former CMO & Head of Medical
  Affairs; advisor to biopharma founders, investors, and medical affairs leaders;
  $5B+ in exits. Fractional CEO/CMO for preclinical-to-Phase-2 biotechs.
- Author of "Dr. Ike's Triangle of Leadership: How to Attract, Move, and Scale People".
- Signature framing: helping teams translate science into strategy and market
  impact — "turn congress signal into strategy", "turning complex clinical data
  into credible, fundable strategy".
- Therapeutic areas he actually covers: obesity/cardiometabolic (GLP-1s, amylin,
  MASH), nephrology and kidney health, rare and specialty disease, immunology
  (myasthenia gravis, thyroid eye disease, CIDP), diabetes; plus AI in pharma and
  clinical-trial diversity and access (including sub-Saharan Africa).

VOICE (from his real posts):
- Hook first: open with the tension or headline, then unpack it ("The buzz around
  Lilly's −29% weight-loss headline is everywhere, but let's dive into the details.").
- Data-precise and skeptical-but-fair: exact numbers, sample-size caveats, lines
  like "deserves an asterisk" and "signal worth tracking, not yet a story to tell".
- Measured, credible, warm. Never hypey or clickbait. No exaggerated claims. Avoid
  anything that reads as medical advice or a regulatory/efficacy claim about a product.
- Almost no emoji in the caption body.
- ALWAYS include 2 to 5 hashtags (they drive viewership): mix one or two
  high-reach tags (#Biopharma, #Leadership, #DrugDevelopment) with niche tags
  specific to the post's topic. Hashtags go in the hashtags field, not the caption.

MATCH HIS STYLE using these real recent posts as reference for length, structure,
and tone:
{recent_posts}

{task}

Your FINAL message must be ONLY valid JSON, no preamble, no markdown fences:
{{"caption":"...", "hashtags":"2 to 5 hashtags separated by spaces", "image_text":"a quotable line <=140 chars", "photo": <library photo number, or null>}}"""

REQUIRED_KEYS = ("caption", "hashtags", "image_text")

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}


def _load_recent_posts() -> str:
    try:
        return config.SAMPLE_POSTS_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _build_prompt(category: str, source_theme: str | None,
                  avoid_captions: list[str] | None,
                  lessons: list[str] | None = None,
                  top_performers: list[tuple[int, str]] | None = None,
                  photos: list[tuple[int, str]] | None = None) -> str:
    brief = CATEGORIES[category]["brief"]
    task = f"TASK: Write ONE post. Today's category: {brief}"
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
    return PROMPT_TEMPLATE.format(recent_posts=_load_recent_posts(), task=task)


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
                  photos: list[tuple[int, str]] | None = None) -> dict:
    """Returns {caption, hashtags, image_text, photo}. Raises RuntimeError on failure.

    category: key from CATEGORIES.
    source_theme: optional steering note from Dr. Ike (redo notes, cron themes).
    avoid_captions: captions of the other drafts in today's batch.
    lessons: durable style rules distilled from his past corrections.
    top_performers: [(engagement_score, caption)] of his best-performing posts.
    photos: [(index, description)] of available library photos; the returned
        "photo" is the chosen index, or None to use the branded quote card.
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category}")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
    prompt = _build_prompt(category, source_theme, avoid_captions, lessons,
                           top_performers, photos)
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
