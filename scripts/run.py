from __future__ import annotations

import html as html_lib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from dateutil import parser as dateparser
from zoneinfo import ZoneInfo


FEED_URL = "https://bits.debian.org/feeds/atom.xml"
SITE_BASE = "https://bits.debian.org/"
DOCS_DIR = Path("docs")
HISTORY_FILE = DOCS_DIR / "feed-history.json"
INDEX_FILE = DOCS_DIR / "index.html"

MAX_HISTORY = 6
TRANSLATE_THRESHOLD = 6000
TRANSLATE_SEGMENT_SIZE = 5500
TRANSLATE_RETRY_TIMES = 3

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
UTC8_LABEL = "UTC+8"
USER_AGENT = "bits-debian-newsletter/1.0 (+https://github.com/)"

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
MAILEROO_API_URL = os.getenv("MAILEROO_API_URL", "https://smtp.maileroo.com/send")


COMMON_STYLE_MAP = {
    "p": "margin:0 0 14px 0;",
    "ul": "margin:0 0 14px 20px; padding:0;",
    "ol": "margin:0 0 14px 20px; padding:0;",
    "li": "margin:0 0 8px 0;",
    "blockquote": "margin:0 0 14px 0; padding:0 0 0 12px; border-left:4px solid #CBD5E1;",
    "h1": "margin:0 0 12px 0; font-size:22px; line-height:1.4;",
    "h2": "margin:0 0 12px 0; font-size:20px; line-height:1.4;",
    "h3": "margin:0 0 10px 0; font-size:18px; line-height:1.4;",
    "h4": "margin:0 0 10px 0; font-size:16px; line-height:1.4;",
    "h5": "margin:0 0 8px 0; font-size:15px; line-height:1.4;",
    "h6": "margin:0 0 8px 0; font-size:14px; line-height:1.4;",
    "pre": "white-space:pre-wrap; word-break:break-word; overflow-wrap:anywhere; background:#F3F4F6; padding:12px; border-radius:8px; margin:0 0 14px 0;",
    "code": "word-break:break-word; font-family:Consolas,Monaco,monospace; background:#F3F4F6; padding:1px 4px; border-radius:4px;",
    "figure": "margin:0 0 14px 0;",
    "figcaption": "margin:8px 0 0 0; color:#6B7280; font-size:12px; line-height:1.6;",
    "table": "width:100% !important; border-collapse:collapse; margin:0 0 14px 0;",
    "th": "border:1px solid #E5E7EB; padding:8px; text-align:left;",
    "td": "border:1px solid #E5E7EB; padding:8px; text-align:left;",
    "hr": "border:none; border-top:1px solid #E5E7EB; margin:18px 0;",
}


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_recipients(raw: str) -> list[str]:
    recipients = [x.strip() for x in re.split(r"[,\n;]+", raw or "") if x.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_TO is empty after parsing.")
    return recipients


def ensure_http_ok(resp: requests.Response, service_name: str) -> None:
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(f"{service_name} HTTP {resp.status_code}: {resp.text}") from exc


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def parse_datetime_any(value: str | None) -> datetime:
    if value:
        try:
            dt = dateparser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    return datetime.now(timezone.utc)


def normalize_style(style: str, remove_keys: set[str] | None = None) -> str:
    remove_keys = {x.lower() for x in (remove_keys or set())}
    parts: list[str] = []
    for item in (style or "").split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        if key.lower() in remove_keys:
            continue
        parts.append(f"{key}: {value}")
    return "; ".join(parts)


def merge_style(existing: str, addition: str, remove_keys: set[str] | None = None) -> str:
    base = normalize_style(existing, remove_keys=remove_keys)
    addition = addition.strip()
    if base and not base.endswith(";"):
        base += ";"
    if base and addition:
        return f"{base} {addition}"
    return base or addition


def sanitize_html(fragment: str, base_url: str) -> str:
    if not fragment:
        return ""

    soup = BeautifulSoup(fragment, "html.parser")

    for bad in soup(["script", "style", "iframe", "object", "embed"]):
        bad.decompose()

    for tag in soup.find_all(True):
        if tag.name == "img":
            src = tag.get("src")
            if src:
                tag["src"] = urljoin(base_url, src)
            for attr in ("width", "height", "srcset", "sizes"):
                tag.attrs.pop(attr, None)
            tag["style"] = merge_style(
                tag.get("style", ""),
                "max-width:100% !important; height:auto !important; display:block; margin:12px auto; border:0; outline:none; text-decoration:none;",
                remove_keys={"width", "height", "max-width", "max-height", "min-width", "min-height"},
            )
            if "alt" not in tag.attrs:
                tag["alt"] = ""

        elif tag.name == "a":
            href = tag.get("href")
            if href:
                tag["href"] = urljoin(base_url, href)
            tag["style"] = merge_style(
                tag.get("style", ""),
                "color:#2563EB; text-decoration:underline; word-break:break-word;",
            )

        if tag.name in COMMON_STYLE_MAP:
            tag["style"] = merge_style(tag.get("style", ""), COMMON_STYLE_MAP[tag.name])

    return str(soup).strip()


def fetch_latest_posts() -> list[dict[str, Any]]:
    print("Fetching Atom feed...")
    resp = requests.get(
        FEED_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    ensure_http_ok(resp, "Feed")

    feed = feedparser.parse(resp.text)
    entries = feed.entries[:2]
    if not entries:
        raise RuntimeError("No entries found in feed.")

    posts: list[dict[str, Any]] = []
    for entry in entries:
        title = (entry.get("title") or "Untitled").strip()
        link = urljoin(SITE_BASE, entry.get("link", ""))
        published_raw = entry.get("published") or entry.get("updated") or ""
        published_dt = parse_datetime_any(published_raw)

        content_html = ""
        if entry.get("content"):
            content_html = entry.content[0].value
        elif entry.get("summary"):
            content_html = entry.summary

        sanitized = sanitize_html(content_html, link or SITE_BASE)

        posts.append(
            {
                "title": title,
                "link": link,
                "published": published_raw or published_dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
                "published_iso": published_dt.astimezone(timezone.utc).isoformat(),
                "content_html": sanitized,
            }
        )

    print(f"Fetched {len(posts)} post(s).")
    return posts


def html_text_length(fragment: str) -> int:
    soup = BeautifulSoup(fragment or "", "html.parser")
    return len(soup.get_text(" ", strip=True))


def split_plain_text(text: str, max_chars: int) -> list[str]:
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
            current = ""

    for para in paragraphs:
        units = [u.strip() for u in re.split(r"(?<=[。！？!?\.])\s+|(?<=;)\s+", para) if u.strip()]
        if not units:
            units = [para]

        for unit in units:
            candidate = f"{current}\n\n{unit}".strip() if current else unit
            if len(candidate) <= max_chars:
                current = candidate
                continue

            flush()

            if len(unit) <= max_chars:
                current = unit
                continue

            words = unit.split()
            if not words:
                for i in range(0, len(unit), max_chars):
                    chunks.append(unit[i : i + max_chars])
                continue

            temp = ""
            for word in words:
                w_candidate = f"{temp} {word}".strip() if temp else word
                if len(w_candidate) <= max_chars:
                    temp = w_candidate
                else:
                    if temp:
                        chunks.append(temp)
                    temp = word
            if temp:
                current = temp

    flush()
    return chunks


def meaningful_children(tag: Tag) -> list[Any]:
    children: list[Any] = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            if str(child).strip():
                children.append(child)
        elif isinstance(child, Tag):
            children.append(child)
    return children


def attrs_to_html(tag: Tag) -> str:
    parts: list[str] = []
    for key, value in tag.attrs.items():
        if value is None:
            continue
        if isinstance(value, list):
            value = " ".join(str(v) for v in value)
        parts.append(f' {key}="{html_lib.escape(str(value), quote=True)}"')
    return "".join(parts)


def node_text_length(node: Any) -> int:
    if isinstance(node, NavigableString):
        return len(str(node).strip())
    if isinstance(node, Tag):
        return len(node.get_text(" ", strip=True))
    return 0


def pack_blocks(blocks: list[str], max_chars: int) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    current_len = 0

    for block in blocks:
        block_len = max(1, html_text_length(block))
        if current and current_len + block_len > max_chars:
            groups.append(current)
            current = [block]
            current_len = block_len
        else:
            current.append(block)
            current_len += block_len

    if current:
        groups.append(current)

    return groups


def flatten_node_to_blocks(node: Any, max_chars: int) -> list[str]:
    if isinstance(node, NavigableString):
        text = str(node).strip()
        if not text:
            return []
        parts = split_plain_text(text, max_chars)
        return [f"<p>{html_lib.escape(part).replace('\n', '<br/>')}</p>" for part in parts]

    if not isinstance(node, Tag):
        return []

    if node_text_length(node) <= max_chars:
        return [str(node)]

    children = meaningful_children(node)
    if children:
        child_blocks: list[str] = []
        for child in children:
            child_blocks.extend(flatten_node_to_blocks(child, max_chars))

        if not child_blocks:
            return [str(node)]

        wrapper_tags = {
            "div",
            "section",
            "article",
            "ul",
            "ol",
            "blockquote",
            "figure",
            "table",
            "thead",
            "tbody",
            "tfoot",
            "tr",
        }

        if node.name in wrapper_tags:
            groups = pack_blocks(child_blocks, max_chars)
            attr_html = attrs_to_html(node)
            return [f"<{node.name}{attr_html}>{''.join(group)}</{node.name}>" for group in groups]

        return child_blocks

    text = node.get_text("\n", strip=True)
    text_parts = split_plain_text(text, max_chars)
    wrap_tag = node.name if node.name in {"p", "li", "blockquote", "pre"} else "p"
    blocks: list[str] = []
    for part in text_parts:
        if wrap_tag == "pre":
            body = html_lib.escape(part)
        else:
            body = html_lib.escape(part).replace("\n", "<br/>")
        blocks.append(f"<{wrap_tag}>{body}</{wrap_tag}>")
    return blocks


def split_html_for_translation(fragment: str, max_chars: int = TRANSLATE_SEGMENT_SIZE) -> list[str]:
    if not fragment:
        return [""]

    soup = BeautifulSoup(fragment, "html.parser")
    blocks: list[str] = []

    for child in soup.contents:
        blocks.extend(flatten_node_to_blocks(child, max_chars))

    blocks = [b for b in blocks if b.strip()]
    if not blocks:
        return [fragment]

    groups = pack_blocks(blocks, max_chars)
    return ["".join(group) for group in groups if group]


def strip_markdown_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def extract_assets(fragment: str) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(fragment or "", "html.parser")
    hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    imgs = [img.get("src", "") for img in soup.find_all("img", src=True)]
    return hrefs, imgs


def validate_preserved_assets(original_html: str, translated_html: str) -> None:
    original_hrefs, original_imgs = extract_assets(original_html)
    translated_hrefs, translated_imgs = extract_assets(translated_html)

    for href in original_hrefs:
        if href not in translated_hrefs:
            raise ValueError(f"Missing href after translation: {href}")

    for src in original_imgs:
        if src not in translated_imgs:
            raise ValueError(f"Missing img src after translation: {src}")


def call_gemini(prompt: str, api_key: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
        },
    }

    resp = requests.post(
        url,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        json=payload,
        timeout=180,
    )
    ensure_http_ok(resp, "Gemini")

    data = resp.json()
    texts: list[str] = []
    for candidate in data.get("candidates", []):
        content = candidate.get("content", {})
        for part in content.get("parts", []):
            if "text" in part and part["text"]:
                texts.append(part["text"])

    result = "\n".join(texts).strip()
    if not result:
        raise RuntimeError(f"Gemini returned empty result: {json.dumps(data, ensure_ascii=False)[:1200]}")
    return result


def translate_segment_once(segment_html: str, api_key: str) -> str:
    if not segment_html.strip():
        return ""

    if html_text_length(segment_html) == 0:
        return segment_html

    prompt = f"""Translate the following HTML fragment from English to Simplified Chinese.

Strict requirements:
1. Preserve all HTML tags and the original order.
2. Do NOT change, shorten, remove, or rewrite any href/src URL.
3. Preserve all links and img tags.
4. Do NOT translate code, shell commands, file names, URLs, or HTML attribute values.
5. Return HTML only.
6. Do NOT use Markdown code fences.
7. Do NOT add notes or explanations.

HTML:
{segment_html}
"""
    translated = call_gemini(prompt, api_key)
    translated = strip_markdown_fences(translated)
    validate_preserved_assets(segment_html, translated)
    return sanitize_html(translated, SITE_BASE)


def translate_segment_with_retry(segment_html: str, api_key: str) -> str:
    last_error: Exception | None = None

    for attempt in range(1, TRANSLATE_RETRY_TIMES + 1):
        try:
            print(f"Translating segment, attempt {attempt}/{TRANSLATE_RETRY_TIMES}...")
            return translate_segment_once(segment_html, api_key)
        except Exception as exc:
            last_error = exc
            print(f"Segment translation failed: {exc}")
            if attempt < TRANSLATE_RETRY_TIMES:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"Segment translation failed after retries: {last_error}")


def translate_article_html(fragment: str, api_key: str) -> str:
    if not fragment.strip():
        return ""

    length = html_text_length(fragment)
    print(f"Article text length: {length}")

    if length < TRANSLATE_THRESHOLD:
        return translate_segment_with_retry(fragment, api_key)

    segments = split_html_for_translation(fragment, TRANSLATE_SEGMENT_SIZE)
    print(f"Long article detected, split into {len(segments)} segment(s).")

    translated_parts: list[str] = []
    for index, segment in enumerate(segments, start=1):
        print(f"Translating segment {index}/{len(segments)}")
        translated_parts.append(translate_segment_with_retry(segment, api_key))

    return "".join(translated_parts)


def load_history() -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda x: parse_datetime_any(x.get("published_iso") or x.get("published")),
        reverse=True,
    )


def save_history(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    existing = load_history()
    merged: dict[str, dict[str, Any]] = {
        item.get("link", f"unknown-{i}"): item for i, item in enumerate(existing)
    }

    for post in posts:
        merged[post["link"]] = {
            "title": post["title"],
            "link": post["link"],
            "published": post["published"],
            "published_iso": post["published_iso"],
        }

    records = sort_records(list(merged.values()))[:MAX_HISTORY]
    HISTORY_FILE.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return records


def build_history_index(records: list[dict[str, Any]], updated_at_bj: datetime) -> str:
    cards = []
    for item in records:
        title = html_lib.escape(item.get("title", ""))
        link = html_lib.escape(item.get("link", ""), quote=True)
        published = html_lib.escape(item.get("published", ""))

        cards.append(
            f"""
            <li class="card">
              <div class="row">
                <span class="tag">&lt;title&gt;</span>
                <span class="value">{title}</span>
              </div>
              <div class="row">
                <span class="tag">&lt;link&gt;</span>
                <a class="value link" href="{link}" target="_blank" rel="noopener noreferrer">{link}</a>
              </div>
              <div class="row">
                <span class="tag">&lt;published&gt;</span>
                <span class="value">{published}</span>
              </div>
            </li>
            """
        )

    cards_html = "\n".join(cards) if cards else '<li class="card">暂无记录</li>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bits from Debian Records</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --card: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --border: #e2e8f0;
      --accent: #2563eb;
      --header-1: #0F172A;
      --header-2: #1E293B;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .wrap {{
      width: 100%;
      max-width: 860px;
      margin: 0 auto;
      padding: 20px 16px 40px;
    }}
    .hero {{
      background: linear-gradient(135deg, var(--header-1) 0%, var(--header-2) 100%);
      color: #fff;
      border-radius: 18px;
      padding: 24px 20px;
      margin-bottom: 18px;
      box-shadow: 0 10px 25px rgba(15, 23, 42, 0.12);
    }}
    .hero h1 {{
      margin: 0 0 8px 0;
      font-size: 28px;
      line-height: 1.3;
    }}
    .hero p {{
      margin: 0;
      color: #cbd5e1;
      line-height: 1.7;
      font-size: 14px;
    }}
    .meta {{
      margin: 18px 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.7;
    }}
    .list {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 14px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px;
      box-shadow: 0 4px 14px rgba(15, 23, 42, 0.04);
    }}
    .row {{
      display: block;
      margin-bottom: 10px;
      word-break: break-word;
    }}
    .row:last-child {{
      margin-bottom: 0;
    }}
    .tag {{
      display: inline-block;
      min-width: 110px;
      color: #334155;
      font-weight: 700;
      margin-right: 8px;
    }}
    .value {{
      color: #111827;
    }}
    .link {{
      color: var(--accent);
      text-decoration: underline;
    }}
    .footer {{
      margin-top: 20px;
      text-align: center;
      color: #64748b;
      font-size: 13px;
      line-height: 1.7;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>🐧 Bits from Debian Records</h1>
      <p>最近 6 条记录。</p>
    </section>

    <div class="meta">
      Feed: <a href="{FEED_URL}" class="link" target="_blank" rel="noopener noreferrer">{FEED_URL}</a><br>
      Updated at {updated_at_bj.strftime("%Y-%m-%d %H:%M")} {UTC8_LABEL}
    </div>

    <ul class="list">
      {cards_html}
    </ul>

    <div class="footer">
      Generated by GitHub Actions · GitHub Pages
    </div>
  </div>
</body>
</html>
"""


def write_history_index(records: list[dict[str, Any]], updated_at_bj: datetime) -> None:
    INDEX_FILE.write_text(build_history_index(records, updated_at_bj), encoding="utf-8")


def article_card_html(post: dict[str, Any], include_translation: bool) -> str:
    title = html_lib.escape(post["title"])
    link_attr = html_lib.escape(post["link"], quote=True)
    link_text = html_lib.escape(post["link"])
    published = html_lib.escape(post["published"])

    english_html = post.get("content_html", "") or "<p>(No content)</p>"
    translated_html = post.get("translated_html", "") or ""

    chinese_block = ""
    if include_translation and translated_html:
        chinese_block = f"""
        <div style="margin-top:14px; padding:14px 16px; background:#F3F4F6; border-radius:12px;">
          <div style="margin:0 0 10px 0; font-size:13px; font-weight:700; color:#374151;">🤖中文翻译</div>
          <div style="color:#374151; font-size:14px !important; line-height:1.6 !important; word-break:break-word;">
            {translated_html}
          </div>
        </div>
        """

    return f"""
    <tr>
      <td class="px-20" style="padding:0 24px 20px 24px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%; border:1px solid #E5E7EB; border-radius:16px; background:#FFFFFF;">
          <tr>
            <td style="padding:20px;">
              <div style="margin:0 0 8px 0; font-size:22px; line-height:1.4; font-weight:700; color:#0F172A;">
                <a href="{link_attr}" style="color:#0F172A; text-decoration:none;">{title}</a>
              </div>

              <div style="margin:0 0 8px 0; font-size:12px; line-height:1.6; color:#6B7280;">
                Published: {published}
              </div>

              <div style="margin:0 0 16px 0; font-size:13px; line-height:1.6; color:#2563EB; word-break:break-word;">
                <a href="{link_attr}" style="color:#2563EB; text-decoration:underline; word-break:break-word;">{link_text}</a>
              </div>

              <div style="padding:14px 16px; background:#F9FAFB; border-radius:12px;">
                <div style="margin:0 0 10px 0; font-size:13px; font-weight:700; color:#111827;">📄ENGLISH</div>
                <div style="color:#111827; font-size:14px !important; line-height:1.6 !important; word-break:break-word;">
                  {english_html}
                </div>
              </div>

              {chinese_block}
            </td>
          </tr>
        </table>
      </td>
    </tr>
    """


def build_email_html(posts: list[dict[str, Any]], include_translation: bool, updated_at_bj: datetime) -> str:
    cards_html = "\n".join(article_card_html(post, include_translation) for post in posts)
    preview_text = "Latest 2 posts from Bits from Debian with Chinese translation." if include_translation else "Latest 2 posts from Bits from Debian."

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Bits from Debian</title>
  <style>
    body {{
      margin: 0;
      padding: 0;
      background: #F3F4F6;
    }}
    table, td {{
      border-collapse: collapse;
    }}
    img {{
      border: 0;
      outline: none;
      text-decoration: none;
      -ms-interpolation-mode: bicubic;
    }}
    a {{
      text-decoration: underline;
    }}
    @media only screen and (max-width: 600px) {{
      .container {{
        width: 100% !important;
      }}
      .px-20 {{
        padding-left: 16px !important;
        padding-right: 16px !important;
      }}
      .py-24 {{
        padding-top: 20px !important;
        padding-bottom: 20px !important;
      }}
    }}
  </style>
</head>
<body style="margin:0; padding:0; background:#F3F4F6;">
  <div style="display:none; max-height:0; overflow:hidden; opacity:0; mso-hide:all;">
    {html_lib.escape(preview_text)}
  </div>

  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%; background:#F3F4F6;">
    <tr>
      <td align="center" style="padding:24px 12px;">
        <table role="presentation" width="680" cellpadding="0" cellspacing="0" border="0" class="container" style="width:100%; max-width:680px; background:#FFFFFF; border-radius:18px; overflow:hidden;">
          <tr>
            <td class="px-20 py-24" style="padding:28px 24px; background-color:#0F172A; background-image:linear-gradient(135deg, #0F172A 0%, #1E293B 100%);">
              <div style="margin:0; font-size:26px; line-height:1.3; font-weight:700; color:#FFFFFF;">🐧 Bits from Debian</div>
              <div style="margin-top:8px; font-size:13px; line-height:1.7; color:#CBD5E1;">
                Latest 2 posts from bits.debian.org
              </div>
            </td>
          </tr>

          <tr>
            <td class="px-20" style="padding:20px 24px 4px 24px; font-size:14px; line-height:1.7; color:#475569;">
              This email contains the latest 2 entries from the Debian newsletter feed.
            </td>
          </tr>

          {cards_html}

          <tr>
            <td class="px-20 py-24" style="padding:20px 24px; text-align:center; background-color:#0F172A; background-image:linear-gradient(135deg, #1E293B 0%, #0F172A 100%);">
              <div style="font-size:13px; line-height:1.7; color:#CBD5E1; text-align:center;">
                Updated at {updated_at_bj.strftime("%Y-%m-%d %H:%M")} {UTC8_LABEL}
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def html_to_text(fragment: str) -> str:
    soup = BeautifulSoup(fragment or "", "html.parser")
    return soup.get_text("\n", strip=True)


def build_email_text(posts: list[dict[str, Any]], include_translation: bool, updated_at_bj: datetime) -> str:
    lines: list[str] = []

    lines.append("Bits from Debian")
    lines.append("")
    lines.append(f"Updated at {updated_at_bj.strftime('%Y-%m-%d %H:%M')} {UTC8_LABEL}")
    lines.append("")

    for idx, post in enumerate(posts, start=1):
        lines.append(f"{idx}. {post['title']}")
        lines.append(post["link"])
        lines.append(f"Published: {post['published']}")
        lines.append("")
        lines.append("[ENGLISH]")
        lines.append(html_to_text(post.get("content_html", "")))
        lines.append("")
        if include_translation and post.get("translated_html"):
            lines.append("[中文翻译]")
            lines.append(html_to_text(post.get("translated_html", "")))
            lines.append("")
        lines.append("-" * 60)
        lines.append("")

    return "\n".join(lines).strip()


def send_maileroo_email(
    api_key: str,
    email_from: str,
    recipients: list[str],
    subject: str,
    html_body: str,
    text_body: str,
) -> None:
    payload = {
        "from": {
            "address": email_from,
            "name": "Newsletter",
        },
        "to": [{"address": addr} for addr in recipients],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }

    print(f"Sending email to {len(recipients)} recipient(s)...")
    resp = requests.post(
        MAILEROO_API_URL,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "User-Agent": USER_AGENT,
        },
        json=payload,
        timeout=60,
    )
    ensure_http_ok(resp, "Maileroo")
    print("Email sent successfully.")


def main() -> None:
    maileroo_api_key = require_env("MAILEROO_API_KEY")
    email_to = require_env("EMAIL_TO")
    email_from = require_env("EMAIL_FROM")
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()

    recipients = parse_recipients(email_to)
    beijing_now = now_beijing()

    posts = fetch_latest_posts()

    include_translation = False
    if gemini_api_key:
        try:
            for post in posts:
                post["translated_html"] = translate_article_html(post["content_html"], gemini_api_key)
            include_translation = True
            print("All translations completed.")
        except Exception as exc:
            include_translation = False
            print(f"Translation failed, falling back to English-only email: {exc}")
            for post in posts:
                post["translated_html"] = ""
    else:
        print("GEMINI_API_KEY is not set, sending English-only email.")
        for post in posts:
            post["translated_html"] = ""

    records = save_history(posts)
    write_history_index(records, beijing_now)

    subject = f"🐧Bits from Debian - {beijing_now.strftime('%Y-%m-%d')}"
    html_body = build_email_html(posts, include_translation, beijing_now)
    text_body = build_email_text(posts, include_translation, beijing_now)

    send_maileroo_email(
        api_key=maileroo_api_key,
        email_from=email_from,
        recipients=recipients,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
    )

    print("Done.")


if __name__ == "__main__":
    main()
