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
    # ---- X: one sharp idea, built to be reposted ----
    "x_take": {
        "platform": "x",
        "label": "Sharp Take",
        "brief": (
            "a sharp, contrarian-but-true take on leadership or careers — the"
            " kind of line people quote-post because it says what they've felt"
            " but never worded. One idea only. Draw on his Triangle of"
            " Leadership thinking without naming the framework every time."
        ),
        "web_search": False,
    },
    "x_medspace": {
        "platform": "x",
        "label": "Medspace Motivation",
        "brief": (
            "an uplifting one-idea post about the progress happening in"
            " medicine and life sciences — why the work matters, how far"
            " treatment has come. Big-picture and hopeful; no clinical claims,"
            " no numbers that would need a citation, nothing that reads as"
            " medical advice."
        ),
        "web_search": False,
    },
    "x_motivation": {
        "platform": "x",
        "label": "Motivation",
        "brief": (
            "a punchy motivational line about discipline, resilience,"
            " self-belief, or showing up on the hard days — universal, warm,"
            " and short enough to screenshot."
        ),
        "web_search": False,
    },
}

# One-off posts written from uploaded source material (the "Generate a post"
# box). They never occupy a daily slot and are never topped up by the cron.
CUSTOM_CATEGORIES = {
    "custom_ig": {"platform": "instagram", "label": "Custom", "web_search": False},
    "custom_li": {"platform": "linkedin", "label": "Custom", "web_search": False},
    "custom_x": {"platform": "x", "label": "Custom", "web_search": False},
}

# Everything that can appear on a post row — for labels and validity checks.
ALL_CATEGORIES = {**CATEGORIES, **CUSTOM_CATEGORIES}

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

VOICE_X = """VOICE FOR THIS X (TWITTER) POST:
- One idea, stated hard. The caption is the whole post: keep it UNDER 200
  characters so it fits X's limit with the hashtags added after it.
- No threads, no setup, no "here's the thing". Land the line and get out.
- Exactly 2 short hashtags in the hashtags field.
- No emoji, or at most one.
- image_text is still required: a quotable version of the same idea (it
  becomes the branded quote card that rides with the post).
- Still him: a doctor and executive who has lived it, never a growth-hack
  account.

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


PLATFORM_PROMPT_LABELS = {"instagram": "Instagram (cross-posted to Facebook)",
                          "linkedin": "LinkedIn", "x": "X (Twitter)"}


def _voice_block(category: str) -> str:
    platform = ALL_CATEGORIES[category]["platform"]
    if platform == "instagram":
        samples = _read_text(config.BOOK_QUOTES_FILE) or "(none on file)"
        return VOICE_INSTAGRAM.format(samples=samples)
    if platform == "x":
        samples = _read_text(config.BOOK_QUOTES_FILE) or "(none on file)"
        return VOICE_X.format(samples=samples)
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
                  campaign: str | None = None,
                  library: list[tuple[str, str]] | None = None) -> str:
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
            "\n\nDr. Ike wants his REAL photos carrying most of his posts."
            " Choose the photo that best complements this post, set \"photo\""
            " to its number, and write the caption so the photo feels natural"
            " (the image_text field is still required as alt text). Only set"
            " \"photo\" to null when none of them could work at all — the"
            " branded quote card is the fallback, not the default."
        )
    else:
        task += "\n\n(No photos available — set \"photo\" to null.)"
    if library:
        docs = "\n".join(f"- {title}: {summary}" for title, summary in library
                         if title and summary)
        if docs:
            task += (
                "\n\nREFERENCE LIBRARY — material Dr. Ike's team has saved"
                " (articles, features, documents):\n" + docs +
                "\nOnly draw on one of these if it is genuinely relevant to"
                " this post's category — mention it naturally, never force it."
                " Most posts should ignore the library entirely."
            )
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
        platform_label=PLATFORM_PROMPT_LABELS[spec["platform"]],
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
                  campaign: str | None = None,
                  library: list[tuple[str, str]] | None = None) -> dict:
    """Returns {caption, hashtags, image_text, photo}. Raises RuntimeError on failure.

    category: key from CATEGORIES.
    source_theme: optional steering note from Dr. Ike (redo notes, cron themes).
    avoid_captions: captions of the other drafts in today's batch.
    lessons: durable style rules distilled from his past corrections.
    top_performers: [(engagement_score, caption)] of his best-performing posts.
    photos: [(index, description)] of available library photos; the returned
        "photo" is the chosen index, or None to use the branded quote card.
    campaign: instruction of the active posting focus, if one is running.
    library: [(title, summary)] of saved reference material, drawn on only
        when relevant.
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category}")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
    prompt = _build_prompt(category, source_theme, avoid_captions, lessons,
                           top_performers, photos, campaign, library)
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


def generate_custom_post(category: str, source_text: str | None = None,
                         image_path: str | None = None,
                         avoid_captions: list[str] | None = None,
                         lessons: list[str] | None = None,
                         campaign: str | None = None) -> dict:
    """One post in Dr. Ike's voice built on uploaded source material — an
    image, pasted text/link, or both. Used by the "Generate a post" box;
    returns the same {caption, hashtags, image_text} shape as generate_post.
    """
    if category not in CUSTOM_CATEGORIES:
        raise ValueError(f"unknown custom category: {category}")
    spec = CUSTOM_CATEGORIES[category]

    task = (
        "TASK: Dr. Ike's team uploaded source material for a one-off post"
        f" (the {'attached image' if image_path else 'text below'}"
        f"{' and the text below' if image_path and source_text else ''})."
        " Write ONE post in his voice built on that material — announcing it,"
        " reacting to it, or drawing the lesson from it, whichever fits."
        " Stay factual to the material; never invent details it doesn't"
        " contain. If it includes a link, put the link on its own line at the"
        " end of the caption (for Instagram say 'link in bio' instead)."
    )
    if source_text and source_text.strip():
        task += "\n\nSOURCE MATERIAL FROM DR. IKE'S TEAM:\n" + source_text.strip()
    if campaign and campaign.strip():
        task += ("\n\nACTIVE POSTING FOCUS (work it in only where natural):\n"
                 + campaign.strip())
    if lessons:
        rules = "\n".join(f"- {l.strip()}" for l in lessons if l and l.strip())
        if rules:
            task += ("\n\nLESSONS FROM DR. IKE'S PAST CORRECTIONS — follow"
                     " these strictly:\n" + rules)
    if avoid_captions:
        drafts = "\n".join(f"- {c[:200]}" for c in avoid_captions if c and c.strip())
        if drafts:
            task += ("\n\nOther drafts written from this same material are below."
                     " Write something clearly different in angle and opening"
                     " line — same story, different doorway:\n" + drafts)
    task += "\n\n(Set \"photo\" to null — the uploaded image or the quote card is used.)"

    prompt = PROMPT_TEMPLATE.format(
        platform_label=PLATFORM_PROMPT_LABELS[spec["platform"]],
        who=WHO_HE_IS,
        human_rules=HUMAN_RULES,
        voice=_voice_block(category),
        task=task,
    )

    if image_path:
        import base64
        from pathlib import Path
        image_data = base64.standard_b64encode(Path(image_path).read_bytes()).decode()
        content = [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/jpeg",
                        "data": image_data}},
            {"type": "text", "text": prompt},
        ]
    else:
        content = prompt

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
    last_err = None
    for attempt in (1, 2):
        text = _call_claude(client, content, use_search=False)
        try:
            return _extract_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            log.warning("custom attempt %d: response was not valid JSON: %s", attempt, e)
    raise RuntimeError(f"Claude did not return valid post JSON after 2 attempts: {last_err}")


def pick_photo(caption: str, photos: list[tuple[int, str]]) -> int | None:
    """One cheap call: which library photo goes best with this caption?
    Returns the photo index, or None if Claude's answer is unusable."""
    lines = "\n".join(f"{i}. {desc}" for i, desc in photos)
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
    response = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=50,
        messages=[{
            "role": "user",
            "content": (
                "A social post needs a photo. Pick the single photo whose"
                " content and mood best match the post — there is always a"
                " best match; do not decline.\n\nPOST CAPTION:\n" + caption +
                "\n\nPHOTOS:\n" + lines +
                '\n\nReply with ONLY this JSON: {"photo": <number>}'
            ),
        }],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    choice = json.loads(text[start:end + 1]).get("photo")
    return choice if isinstance(choice, int) and not isinstance(choice, bool) else None


def summarize_document(text: str, source_hint: str = "") -> dict:
    """One call at upload time: title + short summary for a library document.
    The summary is what generation prompts see, so it carries the key facts."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
    response = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                "A document was saved to Dr. Ike Ogbaa's content library"
                + (f" (source: {source_hint})" if source_hint else "") +
                ". Title it and summarize it for future social-post writing:"
                " the summary must carry the key facts, names, and anything"
                " quotable, in 2-3 sentences.\n\nDOCUMENT:\n" + text[:12000] +
                '\n\nReply with ONLY this JSON:'
                ' {"title": "short title, max 12 words", "summary": "2-3 sentences"}'
            ),
        }],
    )
    raw = "".join(b.text for b in response.content if b.type == "text")
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON in summary response")
    data = json.loads(raw[start:end + 1])
    title = str(data.get("title", "")).strip() or "Untitled document"
    summary = str(data.get("summary", "")).strip()
    if not summary:
        raise ValueError("empty document summary")
    return {"title": title[:200], "summary": summary[:600]}


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
