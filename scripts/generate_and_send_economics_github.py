"""
Daily economics news generator and SMTP sender.

Designed for unattended execution in GitHub Actions.

Features:
- Fetches RSS news without paid news APIs.
- Keeps the user's economics/business RSS sources.
- Translates English titles/summaries to Simplified Chinese using a
  multi-provider free translation fallback chain.
- Splits long summaries into chunks.
- Retries transient translation/RSS errors.
- Caches translations during the current run to reduce duplicate requests.
- Never replaces an article with a fake "translation unavailable" message:
  if translation fails, the original text is retained.
- Uses environment variables for SMTP and optional translation settings.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from typing import Callable, Optional
from urllib.parse import urlparse

import feedparser
import pytz
import requests
from dateutil import parser as date_parser

# ---------------------------------------------------------------------------
# Optional translation libraries
# Import each provider independently so one missing class/library does not
# disable the other providers.
# ---------------------------------------------------------------------------

try:
    from deep_translator import GoogleTranslator
except Exception:
    GoogleTranslator = None

try:
    from deep_translator import MyMemoryTranslator
except Exception:
    MyMemoryTranslator = None

try:
    from deep_translator import LibreTranslator
except Exception:
    LibreTranslator = None

# Traditional -> Simplified Chinese conversion.
try:
    from opencc import OpenCC

    _opencc = OpenCC("t2s")
except Exception:
    _opencc = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BJT = pytz.timezone("Asia/Shanghai")

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "").strip()
EMAIL_TO = os.getenv("EMAIL_TO", "").strip()

# Optional external translation API.
TRANSLATE_API_URL = os.getenv("TRANSLATE_API_URL", "").strip()
TRANSLATE_API_KEY = os.getenv("TRANSLATE_API_KEY", "").strip()

# Optional LibreTranslate configuration.
LIBRETRANSLATE_URL = os.getenv(
    "LIBRETRANSLATE_URL",
    "https://libretranslate.com",
).strip()
LIBRETRANSLATE_API_KEY = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()

# Runtime controls.
NEWS_LIMIT = int(os.getenv("NEWS_LIMIT", "5"))
SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "800"))
TRANSLATION_CHUNK_SIZE = int(os.getenv("TRANSLATION_CHUNK_SIZE", "450"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
RSS_TIMEOUT = int(os.getenv("RSS_TIMEOUT", "20"))
TRANSLATION_RETRIES = int(os.getenv("TRANSLATION_RETRIES", "2"))
TRANSLATION_DELAY = float(os.getenv("TRANSLATION_DELAY", "0.4"))
RSS_DELAY = float(os.getenv("RSS_DELAY", "0.3"))

USER_AGENT = os.getenv(
    "USER_AGENT",
    "daily-economics-news/2.0 (+GitHub Actions)",
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RSS feeds
# ---------------------------------------------------------------------------

FEEDS = {
    "economics/经济": [
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.economist.com/finance-and-economics/rss.xml",
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",
        "https://www.cnbc.com/id/15839135/device/rss/rss.html",
        "https://www.ft.com/?format=rss",
    ]
}

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
    }
)

# Translation cache: source text + target language -> translated result.
_translation_cache: dict[tuple[str, str], str] = {}

# Page title cache: url -> page title
PAGE_TITLE_CACHE: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def contains_cjk(text: str) -> bool:
    return bool(
        re.search(
            r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3040-\u30FF\u31F0-\u31FF]",
            text or "",
        )
    )


def clean_text(text: str) -> str:
    """Normalize RSS/HTML text without destroying useful punctuation."""
    if not text:
        return ""

    text = html.unescape(str(text))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def to_simplified(text: str) -> str:
    """Convert Traditional Chinese to Simplified Chinese when OpenCC exists."""
    if not text:
        return text

    if _opencc:
        try:
            return _opencc.convert(text)
        except Exception as exc:
            logger.warning("OpenCC conversion failed: %s", exc)

    return text


def looks_like_valid_translation(
    source: str,
    result: Optional[str],
    target: str,
) -> bool:
    """
    Basic sanity check.

    For English -> Chinese, a result with CJK characters is expected.
    For Chinese -> English, a result containing Latin letters is expected.
    Very short source strings are accepted more permissively.
    """
    if not result:
        return False

    result = clean_text(result)
    if not result:
        return False

    source = clean_text(source)

    if target.lower().startswith("zh"):
        if contains_cjk(result):
            return True
        # A one-word/proper-name source can legitimately remain Latin.
        return len(source) <= 8

    if target.lower().startswith("en"):
        return bool(re.search(r"[A-Za-z]", result))

    return True


def split_text_for_translation(text: str, max_chars: int) -> list[str]:
    """
    Split text into reasonably sized chunks.

    Prefer sentence boundaries; fall back to character chunks.
    """
    text = clean_text(text)
    if not text:
        return []

    if len(text) <= max_chars:
        return [text]

    sentences = re.split(r"(?<=[.!?。！？])\s+", text)
    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""

            for start in range(0, len(sentence), max_chars):
                chunks.append(sentence[start:start + max_chars])
            continue

        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = sentence

    if current:
        chunks.append(current)

    return chunks


# ---------------------------------------------------------------------------
# Translation providers
# ---------------------------------------------------------------------------

def translate_with_external_api(text: str, target: str) -> Optional[str]:
    """Optional user-configured translation API."""
    if not (TRANSLATE_API_URL and TRANSLATE_API_KEY):
        return None

    try:
        response = SESSION.post(
            TRANSLATE_API_URL,
            json={"q": text, "target": target},
            headers={"Authorization": f"Bearer {TRANSLATE_API_KEY}"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict):
            result = (
                data.get("translatedText")
                or data.get("translation")
                or data.get("text")
            )
            if result:
                return str(result)

        if isinstance(data, str):
            return data

    except Exception as exc:
        logger.warning("External translation API failed: %s", exc)

    return None


def translate_with_google(text: str, target: str) -> Optional[str]:
    if GoogleTranslator is None:
        return None

    try:
        return GoogleTranslator(source="auto", target=target).translate(text)
    except Exception as exc:
        logger.warning("Google translation failed: %s", exc)
        return None


def translate_with_mymemory(text: str, target: str) -> Optional[str]:
    if MyMemoryTranslator is None:
        return None

    try:
        target_code = "zh-CN" if target.lower().startswith("zh") else "en"
        return MyMemoryTranslator(
            source="auto",
            target=target_code,
        ).translate(text)
    except Exception as exc:
        logger.warning("MyMemory translation failed: %s", exc)
        return None


def translate_with_libretranslate(text: str, target: str) -> Optional[str]:
    if LibreTranslator is None:
        return None

    try:
        target_code = "zh" if target.lower().startswith("zh") else "en"
        return LibreTranslator(
            source="auto",
            target=target_code,
            base_url=LIBRETRANSLATE_URL,
            api_key=LIBRETRANSLATE_API_KEY or None,
        ).translate(text)
    except Exception as exc:
        logger.warning("LibreTranslate failed: %s", exc)
        return None


def provider_chain(target: str) -> list[tuple[str, Callable[[str, str], Optional[str]]]]:
    """
    Provider order.

    A user-configured API gets first priority. Then free providers.
    """
    providers: list[tuple[str, Callable[[str, str], Optional[str]]]] = []

    if TRANSLATE_API_URL and TRANSLATE_API_KEY:
        providers.append(("external-api", translate_with_external_api))

    if GoogleTranslator is not None:
        providers.append(("google", translate_with_google))

    if MyMemoryTranslator is not None:
        providers.append(("mymemory", translate_with_mymemory))

    if LibreTranslator is not None:
        providers.append(("libretranslate", translate_with_libretranslate))

    return providers


def translate_short(text: str, target: str) -> Optional[str]:
    """Translate one reasonably short chunk with retries and fallbacks."""
    text = clean_text(text)
    if not text:
        return ""

    cache_key = (text, target.lower())
    if cache_key in _translation_cache:
        return _translation_cache[cache_key]

    providers = provider_chain(target)

    if not providers:
        logger.error("No translation provider is available.")
        return None

    for provider_name, provider in providers:
        for attempt in range(TRANSLATION_RETRIES + 1):
            result = provider(text, target)

            if looks_like_valid_translation(text, result, target):
                result = clean_text(result)
                _translation_cache[cache_key] = result
                logger.debug(
                    "Translation succeeded: provider=%s target=%s chars=%d",
                    provider_name,
                    target,
                    len(text),
                )
                return result

            if attempt < TRANSLATION_RETRIES:
                delay = TRANSLATION_DELAY * (attempt + 1)
                time.sleep(delay)

        logger.warning(
            "Translation provider exhausted: %s, target=%s",
            provider_name,
            target,
        )

    return None


def translate(text: str, target: str = "zh-CN") -> str:
    """
    Robust translation wrapper.

    - Chinese source + Chinese target: normalize only.
    - English/other source: translate.
    - Long text: split into chunks.
    - Failure: return original text, never a fake error sentence.
    """
    text = clean_text(text)
    if not text:
        return ""

    target_lower = target.lower()

    # Only treat as pure Chinese when it's largely Chinese and not mixed with Latin
    if (
        target_lower.startswith("zh")
        and contains_cjk(text)
        and not re.search(r"[A-Za-z]", text)
    ):
        return to_simplified(text)

    if target_lower.startswith("en") and not contains_cjk(text):
        return text

    chunks = split_text_for_translation(text, TRANSLATION_CHUNK_SIZE)
    if not chunks:
        return text

    translated_chunks: list[str] = []

    for chunk in chunks:
        result = translate_short(chunk, target)

        if result is None:
            logger.error(
                "Translation failed permanently; retaining original chunk."
            )
            translated_chunks.append(chunk)
        else:
            translated_chunks.append(result)

        # Be polite to free translation services.
        time.sleep(TRANSLATION_DELAY)

    result = " ".join(translated_chunks).strip()

    if target_lower.startswith("zh"):
        result = to_simplified(result)

    return result or text


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def parse_time(entry) -> datetime:
    try:
        published_parsed = entry.get("published_parsed")
        if published_parsed:
            return datetime(*published_parsed[:6], tzinfo=pytz.UTC)

        published = entry.get("published")
        if published:
            dt = date_parser.parse(published)
            if dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)
            return dt

    except Exception as exc:
        logger.warning("Failed to parse publication time: %s", exc)

    return datetime.now(pytz.UTC)


def fetch_from_feed(url: str) -> list:
    """Fetch RSS with explicit timeout and retry."""
    for attempt in range(2):
        try:
            response = SESSION.get(url, timeout=RSS_TIMEOUT)
            response.raise_for_status()

            parsed = feedparser.parse(response.content)

            if parsed.bozo:
                logger.warning(
                    "RSS parser warning for %s: %s",
                    url,
                    getattr(parsed, "bozo_exception", "unknown"),
                )

            return parsed.entries or []

        except Exception as exc:
            logger.warning(
                "RSS fetch failed (%d/2): %s | %s",
                attempt + 1,
                url,
                exc,
            )
            if attempt == 0:
                time.sleep(1.5)

    return []


# ---------------------------------------------------------------------------
# News scoring / collection
# ---------------------------------------------------------------------------

def compute_importance(item: dict) -> float:
    score = 0.0

    title = (item.get("title_en") or "").lower()
    source = (item.get("source") or "").lower()

    # Use description when available; otherwise assemble a textual summary.
    desc = (
        item.get("description")
        or f"{item.get('summary_en') or ''} {item.get('summary_zh') or ''}".strip()
    )

    high_sources = [
        "reuters",
        "ft",
        "financial times",
        "bbc",
        "aljazeera",
        "cnn",
        "techcrunch",
        "wired",
        "nature",
        "sciencedaily",
        "bloomberg",
        "cnbc",
        "economist",
    ]

    if any(s in source for s in high_sources):
        score += 3

    keywords = [
        "breaking",
        "breaking news",
        "exclusive",
        "urgent",
        "alert",
        "major",
        "crisis",
        "重大",
        "突发",
        "独家",
        "警报",
        "危机",
    ]

    if any(k in title for k in keywords):
        score += 3

    if len(desc) > 200:
        score += 2

    try:
        published = item.get("published")
        if published:
            age_seconds = (
                datetime.now(pytz.UTC) - published
            ).total_seconds()

            if age_seconds < 86400:
                score += 2

    except Exception:
        pass

    return score


def source_name(entry) -> str:
    source = entry.get("source")

    if isinstance(source, dict):
        return clean_text(source.get("title") or "")

    if source:
        return clean_text(str(source))

    return clean_text(entry.get("author") or "")


def collect_top_items(limit: int = NEWS_LIMIT) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()

    for category, feeds in FEEDS.items():
        for feed_url in feeds:
            entries = fetch_from_feed(feed_url)

            valid_from_this_feed = 0

            for entry in entries:
                link = clean_text(entry.get("link") or entry.get("id") or "")
                if not link or link in seen:
                    continue

                title_raw = clean_text(entry.get("title") or "")
                if not title_raw:
                    continue

                summary_raw = clean_text(
                    entry.get("summary")
                    or entry.get("description")
                    or ""
                )[:SUMMARY_MAX_CHARS]

                seen.add(link)

                published = parse_time(entry)
                source = source_name(entry)

                # Original text.
                title_en = title_raw

                # Translate title.
                title_zh = translate(title_raw, "zh-CN")

                # Translate summary in the appropriate direction.
                if contains_cjk(summary_raw):
                    summary_zh = to_simplified(summary_raw)
                    summary_en = translate(summary_raw, "en")
                else:
                    summary_en = summary_raw
                    summary_zh = translate(summary_raw, "zh-CN")

                item = {
                    "category": category,
                    "title_en": title_en,
                    "title_zh": title_zh,
                    "summary_zh": summary_zh,
                    "summary_en": summary_en,
                    "description": f"{summary_en} {summary_zh}".strip(),
                    "url": link,
                    "published": published,
                    "source": source,
                    "importance": 0.0,
                }

                item["importance"] = compute_importance(item)
                items.append(item)
                valid_from_this_feed += 1

                # Limit valid articles, not merely the first RSS entries.
                if valid_from_this_feed >= 3:
                    break

                if len(items) >= limit:
                    break

            if len(items) >= limit:
                break

            time.sleep(RSS_DELAY)

        if len(items) >= limit:
            break

    # Importance first, publication time second.
    items.sort(
        key=lambda x: (
            x.get("importance", 0),
            x.get("published") or datetime.min.replace(tzinfo=pytz.UTC),
        ),
        reverse=True,
    )

    return items[:limit]


# ---------------------------------------------------------------------------
# Optional page-title fallback
# ---------------------------------------------------------------------------

def fetch_page_title(url: str) -> Optional[str]:
    """Best-effort extraction of an English HTML page title."""
    if url in PAGE_TITLE_CACHE:
        return PAGE_TITLE_CACHE[url]

    try:
        response = SESSION.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        html_text = response.text

        patterns = [
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
        ]

        for pattern in patterns:
            match = re.search(pattern, html_text, flags=re.I)
            if match:
                title = clean_text(match.group(1))
                if title:
                    PAGE_TITLE_CACHE[url] = title
                    return title

        match = re.search(
            r"<title[^>]*>(.*?)</title>",
            html_text,
            flags=re.I | re.S,
        )
        if match:
            title = clean_text(match.group(1))
            if title:
                PAGE_TITLE_CACHE[url] = title
                return title

    except Exception as exc:
        logger.debug("Page title fetch failed for %s: %s", url, exc)

    return None


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def format_bjt(dt: Optional[datetime]) -> str:
    if not dt:
        return ""

    try:
        return dt.astimezone(BJT).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


def build_email_body(items: list[dict]) -> str:
    lines = [
        "日报：全球重要新闻（RSS 源，自动生成）",
        "",
    ]

    for index, item in enumerate(items, 1):
        title_en = item.get("title_en") or ""
        title_zh = item.get("title_zh") or ""
        url = item.get("url") or ""

        # If an RSS provider supplied a Chinese title, try the actual page
        # only as an English-display fallback.
        title_en_display = title_en

        if contains_cjk(title_en):
            fetched = fetch_page_title(url)
            if fetched and not contains_cjk(fetched):
                title_en_display = fetched

        lines.append(str(index))
        lines.append(
            f"   信息源：{item.get('source') or 'unknown'}"
        )
        lines.append(
            f"   发布时间（北京时间）：{format_bjt(item.get('published'))}"
        )
        lines.append(
            f"   重要性评分：{item.get('importance', 0):.1f}"
        )
        lines.append(
            f"   标题（中/英）：{title_zh or '—'} / {title_en_display or '—'}"
        )
        lines.append(
            f"   内容摘要（中）：{item.get('summary_zh', '')}"
        )
        lines.append(
            f"   内容摘要（英）：{item.get('summary_en', '')}"
        )
        lines.append(f"   原文链接：{url}")
        lines.append("")

    return "\n".join(lines)


def send_email(subject: str, body_plain: str) -> None:
    required = {
        "SMTP_HOST": SMTP_HOST,
        "SMTP_USERNAME": SMTP_USERNAME,
        "SMTP_PASSWORD": SMTP_PASSWORD,
        "EMAIL_FROM": EMAIL_FROM,
        "EMAIL_TO": EMAIL_TO,
    }

    missing = [name for name, value in required.items() if not value]

    if missing:
        raise RuntimeError(
            "SMTP configuration is incomplete. Missing: "
            + ", ".join(missing)
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = EMAIL_FROM
    message["To"] = EMAIL_TO
    message.set_content(body_plain)

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            timeout=60,
        ) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(message)
    else:
        with smtplib.SMTP(
            SMTP_HOST,
            SMTP_PORT,
            timeout=60,
        ) as server:
            server.ehlo()
            if server.has_extn("STARTTLS"):
                server.starttls()
                server.ehlo()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(message)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logger.info("Starting daily economics news job.")

    providers = [name for name, _ in provider_chain("zh-CN")]
    logger.info(
        "Translation providers available: %s",
        ", ".join(providers) if providers else "NONE",
    )

    items = collect_top_items(limit=NEWS_LIMIT)

    if not items:
        logger.error(
            "No news items were collected. "
            "RSS sources may be unavailable."
        )
        return 1

    body = build_email_body(items)

    subject = (
        f"每日经济要闻 — "
        f"{datetime.now(BJT).strftime('%Y-%m-%d')}"
    )

    send_email(subject, body)

    logger.info(
        "Completed successfully. Sent %d news items.",
        len(items),
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
