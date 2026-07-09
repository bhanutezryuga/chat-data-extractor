"""Gemini extraction over raw HTTPS (no SDK).

- text/article  -> inline text
- pdf           -> inline base64 PDF (Gemini reads PDFs natively)
- video (V2)    -> YouTube by URL (file_data) or a small inline mp4 (inline_data)

Every call returns the parsed JSON plus `_model` and `_usage` (Gemini's usageMetadata),
so the caller can meter token consumption. Without an API key, `stub()` runs offline.
"""
import base64
import json
import re
import urllib.error
import urllib.request

from . import config

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "{model}:generateContent?key={key}")

SCHEMA_HINT = (
    'Respond ONLY with JSON of this shape: '
    '{"summary": str, "key_points": [str], '
    '"list_items": [{"name": str, "note": str, "link": str}], '
    '"detected_language": str, "suggested_action": "note|list|translate", "translation": str, '
    '"task": {"title": str, "description": str, "suggested_use_case": str, '
    '"priority": "LOW|MEDIUM|HIGH", "tags": [str]}, "confidence": 0.0}')


def _action_instruction():
    return (
        f"Also decide what the user most likely wants done with this (the 'action'):\n"
        f"- `detected_language`: the main language of the content.\n"
        f"- `translation`: a faithful {config.TRANSLATE_TO} translation of the caption/transcript/"
        f"key on-screen text — but ONLY if the content is NOT already in {config.TRANSLATE_TO}; "
        f"otherwise \"\".\n"
        f"- `suggested_action`: \"translate\" if the content is not in {config.TRANSLATE_TO}; "
        f"else \"list\" if it enumerates discrete items; else \"note\".")

LIST_INSTRUCTION = (
    "IMPORTANT: If the content recommends or enumerates discrete items — e.g. books, "
    "products, tools, apps, websites, movies, places, ingredients, tips, or ordered steps — "
    "capture EVERY item in `list_items`, each with its `name` (verbatim, including author/brand "
    "if stated) and a short `note` for any extra detail mentioned (why, price, etc.). Do not "
    "summarize the list away or truncate it. If there is no such list, return [].\n"
    "For each item set `link` to the website/URL **exactly as shown in the content** — read it "
    "from the image frames, on-screen text, or caption (e.g. a URL or @handle printed on a "
    "slide). If no link/URL is actually visible for that item, set `link` to \"link not "
    "available\". NEVER invent, guess, autocomplete, or infer a URL that is not literally shown.")

_MEDIA_RES = {"low": "MEDIA_RESOLUTION_LOW",
              "medium": "MEDIA_RESOLUTION_MEDIUM",
              "high": "MEDIA_RESOLUTION_HIGH"}


def _loads(txt):
    """Parse Gemini JSON tolerantly (strip code fences; fall back to the {...} slice)."""
    txt = txt.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.I).strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        a, b = txt.find("{"), txt.rfind("}")
        if a >= 0 and b > a:
            return json.loads(txt[a:b + 1])
        raise


def _generate(parts, model, media_resolution=None, timeout=300, _retry=True):
    gen = {"responseMimeType": "application/json"}
    if media_resolution:
        gen["mediaResolution"] = media_resolution
    payload = json.dumps({"contents": [{"parts": parts}],
                          "generationConfig": gen}).encode()
    endpoint = ENDPOINT.format(model=model, key=config.GEMINI_API_KEY)
    req = urllib.request.Request(endpoint, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode())
    text_out = out["candidates"][0]["content"]["parts"][0]["text"]
    try:
        data = _loads(text_out)
    except json.JSONDecodeError:
        if _retry:                      # Gemini occasionally emits invalid JSON — regenerate once
            return _generate(parts, model, media_resolution, timeout, _retry=False)
        raise
    data["_model"] = model
    data["_usage"] = out.get("usageMetadata", {})
    return data


def _prompt(rule, url, extra=""):
    return (f"{rule['action_template']}\n\n"
            f"CONTENT TYPE: {rule['content_type']}\nPURPOSE: {rule['purpose']}\n"
            f"SOURCE URL: {url}\n{extra}\n\n{LIST_INSTRUCTION}\n\n{_action_instruction()}\n\n{SCHEMA_HINT}")


def translate(text, to=None):
    """Standalone translation (used when the user overrides to the Translate action)."""
    to = to or config.TRANSLATE_TO
    prompt = (f"Translate the text below into {to}, faithfully. Return ONLY JSON: "
              '{"translation": str, "detected_language": str}.\n\nTEXT:\n' + (text or "")[:8000])
    return _generate([{"text": prompt}], config.GEMINI_MODEL)


def extract(rule, url, text, pdf_bytes=None):
    if pdf_bytes:
        parts = [{"text": _prompt(rule, url, "The attached PDF is the content to analyze.")},
                 {"inline_data": {"mime_type": "application/pdf",
                                  "data": base64.b64encode(pdf_bytes).decode()}}]
    else:
        parts = [{"text": _prompt(rule, url, f"CONTENT:\n{(text or '')[:12000]}")}]
    return _generate(parts, config.GEMINI_MODEL)


def analyze_video(rule, url, video_bytes=None, youtube_url=None):
    """Multimodal video understanding. Provide youtube_url (preferred) or video_bytes."""
    instruction = ("Watch the video (visuals AND audio, including any on-screen text). Describe "
                   "what is demonstrated. Capture EVERY item the creator names or recommends "
                   "(e.g. each book/product/tip) into list_items — read it from the narration and "
                   "any on-screen captions; do not stop at the first few.")
    prompt = _prompt(rule, url, instruction)
    if youtube_url:
        media = {"file_data": {"file_uri": youtube_url}}
    else:
        media = {"inline_data": {"mime_type": "video/mp4",
                                 "data": base64.b64encode(video_bytes).decode()}}
    parts = [{"text": prompt}, media]
    res = _MEDIA_RES.get(config.MEDIA_RESOLUTION)
    try:
        return _generate(parts, config.GEMINI_VIDEO_MODEL, media_resolution=res)
    except urllib.error.HTTPError as e:
        # Some models/versions reject mediaResolution — retry once without it.
        if e.code == 400 and res:
            return _generate(parts, config.GEMINI_VIDEO_MODEL, media_resolution=None)
        raise


def analyze_images(rule, url, images, caption=""):
    """Vision analysis of one or more post images (e.g. an Instagram carousel).
    `images` is a list of raw JPEG/PNG bytes."""
    instruction = ("The attached images are the post (a carousel — read them in order). "
                   "READ ALL TEXT shown in every image and capture each listed item into "
                   "list_items. The caption (context) is below.\n"
                   f"CAPTION: {caption[:1000]}")
    parts = [{"text": _prompt(rule, url, instruction)}]
    for b in images:
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(b).decode()}})
    return _generate(parts, config.GEMINI_MODEL,
                     media_resolution=_MEDIA_RES.get(config.MEDIA_RESOLUTION))


def stub(rule, url, text):
    snippet = (text or url or "").strip()
    summary = (snippet[:200] + "…") if len(snippet) > 200 else \
        (snippet or f"Shared {rule['content_type']}: {url}")
    points = [s.strip() for s in re.split(r"[.!?]\s", snippet) if s.strip()][:3] \
        or ["(no description available)"]
    title = points[0][:60] if points else rule["content_type"].title()
    return {
        "summary": summary,
        "key_points": points,
        "list_items": [],
        "detected_language": "English",
        "suggested_action": "note",
        "translation": "",
        "task": {
            "title": f"Review {rule['content_type']}: {title}",
            "description": f"Look at this {rule['content_type']} and decide a next step.",
            "suggested_use_case": f"Captured from chat; classified as {rule['content_type']}.",
            "priority": "MEDIUM",
            "tags": [rule["content_type"]],
        },
        "confidence": 0.4,
        "_model": "offline-stub",
        "_usage": {},
    }
