#!/usr/bin/env python3
"""Scan selected journals and add new articles to Notion."""

from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import html
import json
import os
import re
import smtplib
import sys
import time
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree


NOTION_VERSION = os.getenv("NOTION_VERSION", "2026-03-11")
USER_AGENT = "literature-scan/1.0 (mailto:you@example.com)"
TEXT_LIMIT = 1900
FOCUS_TOPICS = ["LLM", "AI", "Healthcare", "Policy Evaluation", "Advertising"]
DEFAULT_EMAIL_RECIPIENT = "yu1344@purdue.edu"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


@dataclass(frozen=True)
class Article:
    title: str
    authors: list[str]
    journal: str
    url: str
    doi: str
    published_date: str | None
    abstract: str
    source_type: str


@dataclass(frozen=True)
class ArticleAnalysis:
    english_summary: str
    chinese_summary: str
    keywords: list[str]
    relevance_score: int
    relevance_topics: list[str]
    digest_reason: str


@dataclass(frozen=True)
class CreatedArticle:
    article: Article
    analysis: ArticleAnalysis


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc


def request_text(url: str, timeout: int = 30) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def read_json_env_or_file(env_name: str, file_env_name: str) -> dict[str, Any] | None:
    raw = os.environ.get(env_name)
    if raw:
        return json.loads(raw)
    path = os.environ.get(file_env_name)
    if path and Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def notion_headers() -> dict[str, str]:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise SystemExit("Set NOTION_TOKEN before calling Notion.")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
    }


def notion_schema(journal_names: list[str]) -> dict[str, Any]:
    return {
        "Title": {"title": {}},
        "Authors": {"rich_text": {}},
        "Journal": {
            "select": {"options": [{"name": name} for name in journal_names]},
        },
        "URL": {"url": {}},
        "DOI": {"rich_text": {}},
        "Published Date": {"date": {}},
        "Abstract": {"rich_text": {}},
        "Abstract Summary": {"rich_text": {}},
        "Chinese Summary": {"rich_text": {}},
        "Keywords": {"multi_select": {"options": []}},
        "Relevance Score": {"number": {"format": "number"}},
        "Research Relevance": {
            "multi_select": {"options": [{"name": topic} for topic in FOCUS_TOPICS]},
        },
        "Digest Included": {"checkbox": {}},
        "Digest Reason": {"rich_text": {}},
        "Status": {"select": {"options": [{"name": "Unread"}]}},
        "Source Type": {
            "select": {
                "options": [
                    {"name": "RSS"},
                    {"name": "Publisher Page"},
                    {"name": "Article Page"},
                ]
            }
        },
        "Date Added": {"date": {}},
        "Run ID": {"rich_text": {}},
    }


def create_database(config: list[dict[str, Any]]) -> None:
    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID")
    if not parent_page_id:
        raise SystemExit("Set NOTION_PARENT_PAGE_ID to the Notion page where the database should be created.")

    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"text": {"content": "Literature Scan"}}],
        "initial_data_source": {
            "title": [{"text": {"content": "Articles"}}],
            "properties": notion_schema([journal["name"] for journal in config]),
        },
    }
    response = request_json(
        "POST",
        "https://api.notion.com/v1/databases",
        headers=notion_headers(),
        body=body,
    )
    print(json.dumps(response, indent=2))
    data_source_ids = extract_data_source_ids(response)
    if data_source_ids:
        print("\nSet NOTION_DATA_SOURCE_ID to:")
        print(data_source_ids[0])


def retrieve_data_source() -> dict[str, Any]:
    data_source_id = os.environ.get("NOTION_DATA_SOURCE_ID")
    if not data_source_id:
        raise SystemExit("Set NOTION_DATA_SOURCE_ID before updating Notion schema.")
    return request_json(
        "GET",
        f"https://api.notion.com/v1/data_sources/{data_source_id}",
        headers=notion_headers(),
    )


def ensure_data_source_schema(config: list[dict[str, Any]]) -> None:
    data_source_id = os.environ.get("NOTION_DATA_SOURCE_ID")
    if not data_source_id:
        raise SystemExit("Set NOTION_DATA_SOURCE_ID before updating Notion schema.")

    current = retrieve_data_source()
    existing = current.get("properties", {}) or {}
    desired = notion_schema([journal["name"] for journal in config])
    updates: dict[str, Any] = {}

    for name in (
        "Chinese Summary",
        "Relevance Score",
        "Research Relevance",
        "Digest Included",
        "Digest Reason",
    ):
        if name not in existing:
            updates[name] = desired[name]

    if "Journal" in existing and existing["Journal"].get("type") == "select":
        current_options = [
            option.get("name")
            for option in existing["Journal"].get("select", {}).get("options", [])
            if option.get("name")
        ]
        wanted = [journal["name"] for journal in config]
        if any(name not in current_options for name in wanted):
            merged = current_options + [name for name in wanted if name not in current_options]
            updates["Journal"] = {"select": {"options": [{"name": name} for name in merged]}}

    if not updates:
        return

    request_json(
        "PATCH",
        f"https://api.notion.com/v1/data_sources/{data_source_id}",
        headers=notion_headers(),
        body={"properties": updates},
    )
    print(f"Updated Notion data source schema: {', '.join(updates)}")


def extract_data_source_ids(database_response: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("data_sources", "dataSources"):
        for item in database_response.get(key, []) or []:
            item_id = item.get("id")
            if item_id:
                ids.append(item_id)
    initial = database_response.get("initial_data_source") or database_response.get("initialDataSource")
    if isinstance(initial, dict) and initial.get("id"):
        ids.append(initial["id"])
    return ids


def load_journals(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise ValueError("Journal config must be a JSON list.")
    return data


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def truncate_text(value: str, limit: int = TEXT_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def normalize_doi(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    return value.lower()


def article_key(article: Article) -> str:
    if article.doi:
        return f"doi:{normalize_doi(article.doi)}"
    if article.url:
        return f"url:{article.url.lower()}"
    return f"title:{article.journal}:{article.title.lower()}"


def crossref_date(work: dict[str, Any]) -> str | None:
    for field in ("published-print", "published-online", "published", "issued", "created"):
        parts = (work.get(field) or {}).get("date-parts") or []
        if not parts or not parts[0]:
            continue
        year, month, day = (parts[0] + [1, 1])[:3]
        try:
            return dt.date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            continue
    return None


def crossref_authors(work: dict[str, Any]) -> list[str]:
    authors = []
    for author in work.get("author", []) or []:
        name = " ".join(part for part in [author.get("given"), author.get("family")] if part)
        if name:
            authors.append(name)
    return authors


def fetch_crossref_articles(journal: dict[str, Any], since: dt.date, rows: int) -> list[Article]:
    articles: list[Article] = []
    for issn in journal.get("issns", []):
        params = {
            "filter": f"issn:{issn},type:journal-article,from-index-date:{since.isoformat()}",
            "select": "title,author,container-title,DOI,URL,abstract,published,published-print,published-online,issued,created",
            "sort": "indexed",
            "order": "desc",
            "rows": str(rows),
        }
        url = f"https://api.crossref.org/works?{urlencode(params)}"
        try:
            payload = request_json("GET", url)
        except RuntimeError as exc:
            print(f"Warning: Crossref fetch failed for {journal['name']} ({issn}): {exc}", file=sys.stderr)
            continue
        for work in payload.get("message", {}).get("items", []) or []:
            titles = work.get("title") or []
            title = clean_text(titles[0] if titles else "")
            if not title:
                continue
            articles.append(
                Article(
                    title=title,
                    authors=crossref_authors(work),
                    journal=journal["name"],
                    url=work.get("URL") or "",
                    doi=normalize_doi(work.get("DOI")),
                    published_date=crossref_date(work),
                    abstract=clean_text(work.get("abstract")),
                    source_type="Article Page",
                )
            )
    return articles


def namespaced_text(element: ElementTree.Element, names: tuple[str, ...]) -> str:
    for child in element.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names and child.text:
            return child.text
    return ""


def parse_feed_date(value: str) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def fetch_rss_articles(journal: dict[str, Any], since: dt.date) -> list[Article]:
    articles: list[Article] = []
    for feed_url in journal.get("rss_feeds", []) or []:
        try:
            xml = request_text(feed_url)
            root = ElementTree.fromstring(xml)
        except (RuntimeError, HTTPError, URLError, ElementTree.ParseError) as exc:
            print(f"Warning: RSS fetch failed for {journal['name']} ({feed_url}): {exc}", file=sys.stderr)
            continue

        entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
        for entry in entries:
            title = clean_text(namespaced_text(entry, ("title",)))
            link = namespaced_text(entry, ("link",))
            if not link:
                for child in entry.iter():
                    if child.tag.rsplit("}", 1)[-1].lower() == "link" and child.attrib.get("href"):
                        link = child.attrib["href"]
                        break
            date_value = (
                namespaced_text(entry, ("pubdate", "published", "updated", "date"))
                or namespaced_text(entry, ("dc:date",))
            )
            published = parse_feed_date(date_value)
            if published:
                try:
                    if dt.date.fromisoformat(published) < since:
                        continue
                except ValueError:
                    pass
            description = namespaced_text(entry, ("description", "summary", "content", "encoded"))
            doi = normalize_doi(namespaced_text(entry, ("doi",)))
            if not title:
                continue
            articles.append(
                Article(
                    title=title,
                    authors=[],
                    journal=journal["name"],
                    url=link,
                    doi=doi,
                    published_date=published,
                    abstract=clean_text(description),
                    source_type="RSS",
                )
            )
    return articles


def merge_articles(articles: list[Article]) -> list[Article]:
    merged: dict[str, Article] = {}
    for article in articles:
        key = article_key(article)
        existing = merged.get(key)
        if not existing:
            merged[key] = article
            continue
        merged[key] = Article(
            title=existing.title or article.title,
            authors=existing.authors or article.authors,
            journal=existing.journal,
            url=existing.url or article.url,
            doi=existing.doi or article.doi,
            published_date=existing.published_date or article.published_date,
            abstract=existing.abstract or article.abstract,
            source_type="RSS" if existing.source_type == "RSS" or article.source_type == "RSS" else existing.source_type,
        )
    return list(merged.values())


def fetch_articles(config: list[dict[str, Any]], lookback_days: int, max_articles: int) -> list[Article]:
    since = dt.date.today() - dt.timedelta(days=lookback_days)
    articles: list[Article] = []
    per_journal_rows = max(max_articles, 20)
    for journal in config:
        articles.extend(fetch_rss_articles(journal, since))
        articles.extend(fetch_crossref_articles(journal, since, per_journal_rows))
    merged = merge_articles(articles)
    merged.sort(key=lambda article: article.published_date or "", reverse=True)
    return merged[:max_articles]


def gemini_client() -> Any:
    try:
        from google import genai
        from google.genai.types import HttpOptions
    except ImportError as exc:
        raise SystemExit("Install dependencies first: python -m pip install -r requirements.txt") from exc

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION") or "global"
    if not project:
        raise SystemExit("Set GOOGLE_CLOUD_PROJECT before calling Gemini on Vertex AI.")
    return genai.Client(vertexai=True, project=project, location=location, http_options=HttpOptions(api_version="v1"))


def analyze_article(client: Any, article: Article) -> ArticleAnalysis:
    model = os.getenv("GEMINI_MODEL", "gemini-3.1-pro-preview")
    topics = ", ".join(FOCUS_TOPICS)
    abstract = article.abstract or "Abstract unavailable."
    prompt = f"""
Analyze this academic article for a marketing, management, and operations researcher whose first language is Mandarin.
Return strict JSON with:
- english_summary: two concise English sentences
- chinese_summary: two concise Mandarin Chinese sentences
- keywords: 4 to 8 short topical keywords in English
- relevance_score: integer from 0 to 100 for relevance to these research interests: {topics}
- relevance_topics: zero or more exact labels from this list: {FOCUS_TOPICS}
- digest_reason: one concise English sentence explaining why it is or is not relevant

Title: {article.title}
Journal: {article.journal}
Authors: {"; ".join(article.authors)}
Abstract: {abstract}
""".strip()
    response = client.models.generate_content(model=model, contents=prompt)
    text = getattr(response, "text", "") or ""
    try:
        parsed = json.loads(extract_json(text))
    except json.JSONDecodeError:
        return ArticleAnalysis(
            english_summary=clean_text(text),
            chinese_summary="",
            keywords=[],
            relevance_score=0,
            relevance_topics=[],
            digest_reason="Gemini returned non-JSON output.",
        )
    english_summary = clean_text(str(parsed.get("english_summary", "")))
    chinese_summary = clean_text(str(parsed.get("chinese_summary", "")))
    keywords = [clean_text(str(item)) for item in parsed.get("keywords", []) if clean_text(str(item))]
    raw_score = parsed.get("relevance_score", 0)
    try:
        relevance_score = max(0, min(100, int(raw_score)))
    except (TypeError, ValueError):
        relevance_score = 0
    relevance_topics = [
        clean_text(str(item))
        for item in parsed.get("relevance_topics", [])
        if clean_text(str(item)) in FOCUS_TOPICS
    ]
    digest_reason = clean_text(str(parsed.get("digest_reason", "")))
    return ArticleAnalysis(
        english_summary=english_summary,
        chinese_summary=chinese_summary,
        keywords=keywords[:8],
        relevance_score=relevance_score,
        relevance_topics=relevance_topics,
        digest_reason=digest_reason,
    )


def summarize_weekly_trend(client: Any, created_articles: list[CreatedArticle]) -> tuple[str, str]:
    if not created_articles:
        return "No new papers were added this week.", "本周没有新增论文。"

    model = os.getenv("GEMINI_MODEL", "gemini-3.1-pro-preview")
    article_lines = []
    for item in created_articles:
        article_lines.append(
            f"- {item.article.journal}: {item.article.title}. "
            f"Summary: {item.analysis.english_summary or item.article.abstract[:400]}"
        )
    prompt = f"""
Write a weekly literature trend summary for these newly added papers.
Return strict JSON with:
- english_trend: 2 to 4 concise English sentences
- chinese_trend: 2 to 4 concise Mandarin Chinese sentences

Papers:
{chr(10).join(article_lines[:50])}
""".strip()
    response = client.models.generate_content(model=model, contents=prompt)
    text = getattr(response, "text", "") or ""
    try:
        parsed = json.loads(extract_json(text))
    except json.JSONDecodeError:
        return clean_text(text), ""
    return clean_text(str(parsed.get("english_trend", ""))), clean_text(str(parsed.get("chinese_trend", "")))


def extract_json(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if match:
        return match.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def rich_text(value: str) -> dict[str, Any]:
    value = truncate_text(value)
    return {"rich_text": [{"text": {"content": value}}]} if value else {"rich_text": []}


def page_properties(article: Article, analysis: ArticleAnalysis, run_id: str, digest_included: bool) -> dict[str, Any]:
    props: dict[str, Any] = {
        "Title": {"title": [{"text": {"content": truncate_text(article.title)}}]},
        "Authors": rich_text("; ".join(article.authors)),
        "Journal": {"select": {"name": article.journal}},
        "URL": {"url": article.url or None},
        "DOI": rich_text(article.doi),
        "Published Date": {"date": {"start": article.published_date}} if article.published_date else {"date": None},
        "Abstract": rich_text(article.abstract),
        "Abstract Summary": rich_text(analysis.english_summary),
        "Chinese Summary": rich_text(analysis.chinese_summary),
        "Keywords": {"multi_select": [{"name": keyword} for keyword in analysis.keywords]},
        "Relevance Score": {"number": analysis.relevance_score},
        "Research Relevance": {"multi_select": [{"name": topic} for topic in analysis.relevance_topics]},
        "Digest Included": {"checkbox": digest_included},
        "Digest Reason": rich_text(analysis.digest_reason),
        "Status": {"select": {"name": "Unread"}},
        "Source Type": {"select": {"name": article.source_type}},
        "Date Added": {"date": {"start": dt.date.today().isoformat()}},
        "Run ID": rich_text(run_id),
    }
    return props


def notion_query_existing(article: Article) -> bool:
    data_source_id = os.environ.get("NOTION_DATA_SOURCE_ID")
    if not data_source_id:
        raise SystemExit("Set NOTION_DATA_SOURCE_ID before scanning into Notion.")

    filters = []
    if article.doi:
        filters.append({"property": "DOI", "rich_text": {"equals": article.doi}})
    if article.url:
        filters.append({"property": "URL", "url": {"equals": article.url}})
    if not filters:
        filters.append({"property": "Title", "title": {"equals": article.title}})
    body = {"filter": filters[0] if len(filters) == 1 else {"or": filters}, "page_size": 1}
    response = request_json(
        "POST",
        f"https://api.notion.com/v1/data_sources/{data_source_id}/query",
        headers=notion_headers(),
        body=body,
    )
    return bool(response.get("results"))


def notion_create_page(article: Article, analysis: ArticleAnalysis, run_id: str, digest_included: bool) -> None:
    data_source_id = os.environ.get("NOTION_DATA_SOURCE_ID")
    if not data_source_id:
        raise SystemExit("Set NOTION_DATA_SOURCE_ID before scanning into Notion.")
    body = {
        "parent": {"data_source_id": data_source_id},
        "properties": page_properties(article, analysis, run_id, digest_included),
    }
    request_json("POST", "https://api.notion.com/v1/pages", headers=notion_headers(), body=body)


def selected_digest_articles(created_articles: list[CreatedArticle]) -> list[CreatedArticle]:
    threshold = int(os.getenv("DIGEST_RELEVANCE_THRESHOLD", "60"))
    max_articles = int(os.getenv("DIGEST_MAX_ARTICLES", "20"))
    selected = [
        item
        for item in created_articles
        if item.analysis.relevance_score >= threshold and item.analysis.relevance_topics
    ]
    if not selected and created_articles:
        selected = sorted(created_articles, key=lambda item: item.analysis.relevance_score, reverse=True)[:5]
    return sorted(selected, key=lambda item: item.analysis.relevance_score, reverse=True)[:max_articles]


def build_digest(created_articles: list[CreatedArticle], trend_en: str, trend_zh: str, run_id: str) -> tuple[str, str, str]:
    journal_counts = collections.Counter(item.article.journal for item in created_articles)
    selected = selected_digest_articles(created_articles)
    subject = f"Weekly Literature Digest: {len(created_articles)} new papers, {len(selected)} highlighted"

    lines = [
        "Weekly Literature Digest",
        f"Run ID: {run_id}",
        "",
        "Overview",
        f"- New papers added: {len(created_articles)}",
    ]
    for journal, count in sorted(journal_counts.items()):
        lines.append(f"- {journal}: {count}")
    lines.extend(["", "General trend", trend_en, trend_zh, "", "Most relevant papers"])
    if not selected:
        lines.append("No newly added papers passed the relevance threshold this week.")
    for index, item in enumerate(selected, start=1):
        article = item.article
        analysis = item.analysis
        lines.extend(
            [
                "",
                f"{index}. {article.title}",
                f"Authors: {'; '.join(article.authors) or 'Unavailable'}",
                f"Journal: {article.journal}",
                f"Relevance: {analysis.relevance_score}/100 ({', '.join(analysis.relevance_topics) or 'No focus topic'})",
                f"Chinese summary: {analysis.chinese_summary or '暂无中文总结。'}",
                f"URL: {article.url}",
                "Abstract:",
                article.abstract or "Abstract unavailable.",
            ]
        )

    html_parts = [
        "<html><body>",
        "<h1>Weekly Literature Digest</h1>",
        f"<p><strong>Run ID:</strong> {html.escape(run_id)}</p>",
        "<h2>Overview</h2>",
        f"<p><strong>New papers added:</strong> {len(created_articles)}</p>",
        "<ul>",
    ]
    for journal, count in sorted(journal_counts.items()):
        html_parts.append(f"<li>{html.escape(journal)}: {count}</li>")
    html_parts.extend(
        [
            "</ul>",
            "<h2>General Trend</h2>",
            f"<p>{html.escape(trend_en)}</p>",
            f"<p lang=\"zh-CN\">{html.escape(trend_zh)}</p>",
            "<h2>Most Relevant Papers</h2>",
        ]
    )
    if not selected:
        html_parts.append("<p>No newly added papers passed the relevance threshold this week.</p>")
    for index, item in enumerate(selected, start=1):
        article = item.article
        analysis = item.analysis
        html_parts.extend(
            [
                "<hr>",
                f"<h3>{index}. {html.escape(article.title)}</h3>",
                f"<p><strong>Authors:</strong> {html.escape('; '.join(article.authors) or 'Unavailable')}</p>",
                f"<p><strong>Journal:</strong> {html.escape(article.journal)}</p>",
                f"<p><strong>Relevance:</strong> {analysis.relevance_score}/100 "
                f"({html.escape(', '.join(analysis.relevance_topics) or 'No focus topic')})</p>",
                f"<p><strong>中文简要总结:</strong> <span lang=\"zh-CN\">{html.escape(analysis.chinese_summary or '暂无中文总结。')}</span></p>",
                f"<p><strong>URL:</strong> <a href=\"{html.escape(article.url)}\">{html.escape(article.url)}</a></p>",
                f"<p><strong>Abstract:</strong></p><p>{html.escape(article.abstract or 'Abstract unavailable.')}</p>",
            ]
        )
    html_parts.append("</body></html>")
    return subject, "\n".join(lines), "\n".join(html_parts)


def send_email(subject: str, text_body: str, html_body: str) -> None:
    recipient = os.getenv("EMAIL_RECIPIENT", DEFAULT_EMAIL_RECIPIENT)
    sender = os.getenv("EMAIL_FROM")
    if not sender:
        raise SystemExit("Set EMAIL_FROM before sending digest email.")

    if gmail_api_configured():
        send_gmail_api_message(sender, recipient, subject, text_body, html_body)
        return

    sendgrid_key = os.getenv("SENDGRID_API_KEY")
    if sendgrid_key:
        body = {
            "personalizations": [{"to": [{"email": recipient}]}],
            "from": {"email": sender},
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": text_body},
                {"type": "text/html", "value": html_body},
            ],
        }
        request_json(
            "POST",
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {sendgrid_key}"},
            body=body,
        )
        return

    host = os.getenv("SMTP_HOST")
    if not host:
        raise SystemExit("Set SENDGRID_API_KEY or SMTP_HOST before sending digest email.")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if env_bool("SMTP_USE_TLS", True):
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.sendmail(sender, [recipient], message.as_string())


def gmail_api_configured() -> bool:
    return bool(
        os.getenv("GMAIL_TOKEN_JSON")
        or os.getenv("GMAIL_TOKEN_FILE")
        or Path("gmail-token.json").exists()
    )


def gmail_credentials_from_token() -> Any:
    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise SystemExit("Install dependencies first: python -m pip install -r requirements.txt") from exc

    token_data = read_json_env_or_file("GMAIL_TOKEN_JSON", "GMAIL_TOKEN_FILE")
    token_path = Path(os.getenv("GMAIL_TOKEN_FILE", "gmail-token.json"))
    if token_data is None and token_path.exists():
        token_data = json.loads(token_path.read_text(encoding="utf-8"))
    if token_data is None:
        raise SystemExit("Set GMAIL_TOKEN_JSON or GMAIL_TOKEN_FILE before using Gmail API.")

    creds = Credentials.from_authorized_user_info(token_data, [GMAIL_SEND_SCOPE])
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        if os.getenv("GMAIL_TOKEN_FILE") or token_path.exists():
            write_json(token_path, json.loads(creds.to_json()))
    return creds


def send_gmail_api_message(sender: str, recipient: str, subject: str, text_body: str, html_body: str) -> None:
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise SystemExit("Install dependencies first: python -m pip install -r requirements.txt") from exc

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    service = build("gmail", "v1", credentials=gmail_credentials_from_token())
    service.users().messages().send(userId="me", body={"raw": encoded}).execute()


def gmail_auth() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit("Install dependencies first: python -m pip install -r requirements.txt") from exc

    credentials_data = read_json_env_or_file("GMAIL_CREDENTIALS_JSON", "GMAIL_CREDENTIALS_FILE")
    credentials_path = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "gmail-credentials.json"))
    token_path = Path(os.getenv("GMAIL_TOKEN_FILE", "gmail-token.json"))
    if credentials_data:
        temp_credentials_path = Path(".gmail-credentials.tmp.json")
        write_json(temp_credentials_path, credentials_data)
        credentials_path = temp_credentials_path
    elif not credentials_path.exists():
        raise SystemExit("Set GMAIL_CREDENTIALS_FILE or put OAuth credentials at gmail-credentials.json.")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), [GMAIL_SEND_SCOPE])
        creds = flow.run_local_server(port=0)
        write_json(token_path, json.loads(creds.to_json()))
        print(f"Wrote Gmail token to {token_path}")
        print("For GitHub Actions, add this file's full JSON contents as the GMAIL_TOKEN_JSON secret.")
    finally:
        if credentials_path.name == ".gmail-credentials.tmp.json" and credentials_path.exists():
            credentials_path.unlink()


def scan(
    config: list[dict[str, Any]],
    lookback_days: int,
    max_articles: int,
    dry_run: bool,
    send_digest_email: bool,
) -> None:
    run_id = f"lit-scan-{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    articles = fetch_articles(config, lookback_days, max_articles)
    print(f"Found {len(articles)} candidate articles.")
    client = None if dry_run else gemini_client()
    created = 0
    skipped = 0
    created_articles: list[CreatedArticle] = []

    if not dry_run:
        ensure_data_source_schema(config)

    for article in articles:
        if not dry_run and notion_query_existing(article):
            skipped += 1
            print(f"Skip existing: {article.title}")
            continue

        analysis = ArticleAnalysis("", "", [], 0, [], "")
        if client is not None:
            analysis = analyze_article(client, article)
            time.sleep(0.2)

        if dry_run:
            print(json.dumps({"article": article.__dict__, "analysis": analysis.__dict__}, indent=2, ensure_ascii=False))
        else:
            created_item = CreatedArticle(article, analysis)
            threshold = int(os.getenv("DIGEST_RELEVANCE_THRESHOLD", "60"))
            digest_included = analysis.relevance_score >= threshold and bool(analysis.relevance_topics)
            notion_create_page(article, analysis, run_id, digest_included)
            created_articles.append(created_item)
            created += 1
            print(f"Created: {article.title}")
            time.sleep(0.35)

    if not dry_run and send_digest_email:
        trend_en, trend_zh = summarize_weekly_trend(client, created_articles) if client else ("", "")
        subject, text_body, html_body = build_digest(created_articles, trend_en, trend_zh, run_id)
        send_email(subject, text_body, html_body)
        print(f"Sent digest email to {os.getenv('EMAIL_RECIPIENT', DEFAULT_EMAIL_RECIPIENT)}")

    print(f"Done. Created {created}; skipped {skipped}; run_id={run_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Weekly literature scanner for Notion.")
    parser.add_argument(
        "--config",
        default=os.getenv("JOURNAL_CONFIG", "config/journals.json"),
        help="Path to journal config JSON.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create-database", help="Create the Notion database and initial data source.")
    subparsers.add_parser("ensure-schema", help="Add missing Notion properties for the current scanner.")
    subparsers.add_parser("gmail-auth", help="Authorize Gmail API and write gmail-token.json.")

    scan_parser = subparsers.add_parser("scan", help="Scan journals and create Notion entries.")
    scan_parser.add_argument("--lookback-days", type=int, default=int(os.getenv("LOOKBACK_DAYS", "14")))
    scan_parser.add_argument("--max-articles", type=int, default=int(os.getenv("MAX_ARTICLES", "50")))
    scan_parser.add_argument("--dry-run", action="store_true")
    scan_parser.add_argument("--send-email", action="store_true", help="Send a weekly digest email after scanning.")
    return parser


def main() -> None:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    config = load_journals(Path(args.config))

    if args.command == "create-database":
        create_database(config)
    elif args.command == "ensure-schema":
        ensure_data_source_schema(config)
    elif args.command == "gmail-auth":
        gmail_auth()
    elif args.command == "scan":
        send_digest_email = args.send_email or env_bool("EMAIL_ON_SCAN", False)
        scan(config, args.lookback_days, args.max_articles, args.dry_run, send_digest_email)


if __name__ == "__main__":
    main()
