from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "generated" / "site"
RUNS = ROOT / "runs"
SOURCE_REF_RE = re.compile(r"\[S(\d+)\]")
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", re.MULTILINE)
DASH_RE = re.compile(r"[—–]|--")
LOCAL_SOURCE_IDS = {"global_bc", "cbc_bc", "bc_news", "vancouver_news", "vancouver_grants", "vancouver_consultations", "translink"}
MIN_NEWS_CHARS = 800
MAX_NEWS_CHARS = 1500
MIN_NEWS_ITEMS_PER_SECTION = 1
MAX_NEWS_ITEMS_PER_SECTION = 3
MAX_HEADINGS_PER_ITEM = 2
TTS_CHUNK_MAX_BYTES = 4000
TTS_SENTENCE_MAX_BYTES = 900
TTS_MAX_ATTEMPTS = 4
FALLBACK_LOOKBACK_DAYS = 7
SECTION_GENERATION_ATTEMPTS = 2
POST_REVIEW_REPAIR_ATTEMPTS = 2
GEMINI_REVIEW_MAX_ATTEMPTS = 4
GEMINI_REVIEW_RETRY_STATUSES = {429, 500, 502, 503, 504}
FINANCE_PRIORITY_TERMS = (
    "interest rate",
    "rates",
    "bank of canada",
    "federal reserve",
    "fed",
    "inflation",
    "cpi",
    "consumer price",
    "jobs",
    "employment",
    "unemployment",
    "gdp",
    "mortgage",
    "rent",
    "利率",
    "加拿大央行",
    "美联储",
    "通胀",
    "消费者价格",
    "就业",
    "失业",
    "房贷",
)
IMPACT_PREFIX_RE = re.compile(r"这个对于[^\n]{1,80}的影响是[：:]")
NEWS_ANALYST_SYSTEM_PROMPT = """# System Prompt: 新闻解读员

你是一位帮普通人读懂新闻的解读员。不堆砌术语，不卖弄深度，就是把一件事讲清楚、讲透。

## 结构模板

### 标题

鲜明有力，直接点出观点或核心议题。

### 开头（引述与破题）

一句话概括：发生了什么事？
直接写出你的分析：这件事意味着什么？不要用标签式提示语。

### 主体（分析与论证）

原因分析：
为什么会发生这件事？根源是什么？政策推动、利益驱动、技术变革或社会需求分别是什么？

影响评估：
会带来什么好的影响？会带来什么坏的影响？对普通人有什么具体影响，包括钱、时间、选择和生活？

本质洞察：
这件事映射了什么更大的趋势或规律？类似的事情以前发生过吗？别的地方有吗？

### 结尾（总结与展望）

先用“最后，总的来说，xxxxxx”收住全文。
提出解决办法或理性呼吁。
body_markdown 到这里结束，里面不得出现影响块。
影响块必须单独放在 JSON 的 impact_markdown 字段，固定格式：
这个对于[具体群体]的影响是：
[短期] / [中期] / [长期]

## 风格规则

用高中生的词汇水平。
每段 5-7 句话，不要太长。
不用专业术语，必须出现时加一句解释。
不用破折号。
像跟朋友聊天一样解释，不是写报告。"""


class GeminiReviewUnavailable(RuntimeError):
    """Raised when Gemini evidence review is temporarily unavailable after retries."""


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_date_text(date_text: str) -> str:
    raw = str(date_text or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if not match:
        raise ValueError(f"Invalid date '{date_text}'. Use YYYY-MM-DD, for example 2026-09-17.")
    year, month, day = (int(part) for part in match.groups())
    return dt.date(year, month, day).isoformat()


def vancouver_window(date_text: str, timezone: str) -> tuple[dt.datetime, dt.datetime]:
    zone = ZoneInfo(timezone)
    start = dt.datetime.fromisoformat(normalize_date_text(date_text)).replace(tzinfo=zone)
    return start, start + dt.timedelta(days=1)


def entry_datetime(entry: dict) -> dt.datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)


def collect_candidates(
    source_registry: dict,
    date_text: str,
    timezone: str,
    lookback_days: int = 1,
) -> list[dict]:
    publication_start, end = vancouver_window(date_text, timezone)
    start = publication_start - dt.timedelta(days=max(lookback_days - 1, 0))
    candidates: list[dict] = []
    source_debug: list[dict] = []

    for source in source_registry.get("sources", []):
        if not source.get("enabled") or not source.get("feed_url"):
            continue

        debug = {
            "id": source.get("id"),
            "url": source.get("feed_url"),
            "entries": 0,
            "date_matches": 0,
            "error": "",
        }
        source_debug.append(debug)

        try:
            response = requests.get(
                source["feed_url"],
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; InfoGapBot/1.0; +https://github.com/breakinfogap-spec/Infogap_Test)"
                },
                timeout=8,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            debug["error"] = f"{type(exc).__name__}: {exc}"
            continue

        feed = feedparser.parse(response.content)
        debug["entries"] = len(feed.entries)
        for entry in feed.entries:
            published = entry_datetime(entry)
            if published:
                local_published = published.astimezone(ZoneInfo(timezone))
                if not (start <= local_published < end):
                    continue
            else:
                local_published = None

            debug["date_matches"] += 1
            candidate_date = local_published.date().isoformat() if local_published else date_text
            fallback_age_days = max((dt.date.fromisoformat(date_text) - dt.date.fromisoformat(candidate_date)).days, 0)
            candidates.append(
                {
                    "source_id": source["id"],
                    "source_name": source["name"],
                    "topic_candidates": source.get("topics", []),
                    "geography": source.get("geography"),
                    "title": entry.get("title", "").strip(),
                    "url": entry.get("link", "").strip(),
                    "published_at": local_published.isoformat() if local_published else None,
                    "summary": html.unescape(entry.get("summary", "")).strip(),
                    "candidate_date": candidate_date,
                    "fallback_age_days": fallback_age_days,
                    "is_publication_date": fallback_age_days == 0,
                }
            )

    collect_candidates.last_debug = source_debug
    return candidates


collect_candidates.last_debug = []


def date_text_days_ago(date_text: str, days: int) -> str:
    return (dt.date.fromisoformat(normalize_date_text(date_text)) - dt.timedelta(days=days)).isoformat()


def used_source_urls(date_text: str, lookback_days: int = FALLBACK_LOOKBACK_DAYS) -> set[str]:
    urls: set[str] = set()
    for days_ago in range(1, lookback_days):
        section_path = RUNS / date_text_days_ago(date_text, days_ago) / "sections.json"
        if not section_path.exists():
            continue
        try:
            sections = json.loads(section_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for section in sections:
            for item in section.get("news_items", []):
                for citation in item.get("citations", []):
                    url = str(citation.get("url") or "").strip()
                    if url.startswith("http"):
                        urls.add(url)
    return urls


def candidate_text(candidate: dict) -> str:
    return " ".join(
        str(candidate.get(field) or "")
        for field in ("source_name", "title", "summary", "geography", "source_id")
    ).lower()


def candidate_priority(candidate: dict, topic: str = "") -> tuple[int, int, int, str]:
    text = candidate_text(candidate)
    fallback_age = int(candidate.get("fallback_age_days") or 0)
    previously_used = 1 if candidate.get("previously_used") else 0
    finance_priority = 0
    if topic == "finance" or "finance" in candidate.get("topic_candidates", []):
        finance_priority = -1 if any(term in text for term in FINANCE_PRIORITY_TERMS) else 0
    return (previously_used, fallback_age, finance_priority, str(candidate.get("published_at") or ""))


def collect_candidates_for_publication(
    source_registry: dict,
    date_text: str,
    timezone: str,
    lookback_days: int = FALLBACK_LOOKBACK_DAYS,
) -> list[dict]:
    used_urls = used_source_urls(date_text, lookback_days)
    merged: list[dict] = []
    seen_urls: set[str] = set()

    window_candidates = collect_candidates(source_registry, date_text, timezone, lookback_days=lookback_days)
    for candidate in window_candidates:
        url = str(candidate.get("url") or "").strip()
        if not url.startswith("http") or url in seen_urls:
            continue
        fallback_age_days = int(candidate.get("fallback_age_days") or 0)
        is_previously_used = url in used_urls
        if fallback_age_days > 0 and is_previously_used:
            continue
        seen_urls.add(url)
        enriched = dict(candidate)
        enriched["previously_used"] = is_previously_used
        merged.append(enriched)

    return sorted(merged, key=lambda candidate: candidate_priority(candidate))


def section_stub(site: dict, publication_date: str) -> list[dict]:
    return []


def make_slug(title: str, date_text: str) -> str:
    ascii_title = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    if not ascii_title:
        ascii_title = base64.urlsafe_b64encode(title.encode("utf-8"))[:16].decode("ascii").rstrip("=")
    return f"{date_text}/{ascii_title[:70]}"


def simple_markdown_to_html(markdown: str) -> str:
    blocks = []
    for raw in re.split(r"\n\s*\n", markdown.strip()):
        text = raw.strip()
        if not text:
            continue
        if text.startswith("### "):
            blocks.append(f"<h3>{html.escape(text[4:])}</h3>")
        elif text.startswith("## "):
            blocks.append(f"<h2>{html.escape(text[3:])}</h2>")
        else:
            escaped = "<br>".join(html.escape(line) for line in text.splitlines())
            blocks.append(f"<p>{escaped}</p>")
    return "\n".join(blocks)


def topic_ids(site: dict) -> set[str]:
    return {topic["id"] for topic in site.get("topics", [])}


def topic_name(topic_id: str, topics: list[dict]) -> str:
    for topic in topics:
        if topic["id"] == topic_id:
            return topic["name"]
    return topic_id


def topic_description(topic_id: str, topics: list[dict]) -> str:
    for topic in topics:
        if topic["id"] == topic_id:
            return topic.get("description", "")
    return ""


def normalize_topic_id(value: object, site: dict) -> str:
    raw_value = str(value or "").strip()
    for topic in site.get("topics", []):
        if raw_value in (topic["id"], topic["name"]):
            return topic["id"]
    return ""


def add_local_topic_if_needed(candidate: dict) -> list[str]:
    topics = set(candidate.get("topic_candidates") or [])
    geography = str(candidate.get("geography") or "").lower()
    source_id = str(candidate.get("source_id") or "")
    haystack = f"{candidate.get('source_name', '')} {candidate.get('title', '')} {candidate.get('summary', '')}".lower()
    local_terms = ("vancouver", "metro vancouver", "british columbia", " b.c.", "bc ", "b.c.")
    if source_id in LOCAL_SOURCE_IDS or "vancouver" in geography or any(term in haystack for term in local_terms):
        topics.add("local_vancouver")
    return sorted(topics)


def number_candidates(candidates: list[dict], site: dict) -> list[dict]:
    allowed_topics = topic_ids(site)
    numbered = []
    seen_urls = set()
    for candidate in candidates:
        url = str(candidate.get("url") or "").strip()
        if not url.startswith("http") or url in seen_urls:
            continue
        seen_urls.add(url)
        candidate_topics = [topic for topic in add_local_topic_if_needed(candidate) if topic in allowed_topics]
        numbered.append(
            {
                "ref": f"S{len(numbered) + 1}",
                "source_id": candidate.get("source_id", ""),
                "source_name": candidate.get("source_name", ""),
                "topic_candidates": candidate_topics,
                "geography": candidate.get("geography"),
                "title": str(candidate.get("title") or "").strip(),
                "url": url,
                "published_at": candidate.get("published_at"),
                "summary": str(candidate.get("summary") or "").strip(),
                "candidate_date": candidate.get("candidate_date"),
                "fallback_age_days": candidate.get("fallback_age_days", 0),
                "is_publication_date": candidate.get("is_publication_date", True),
                "previously_used": candidate.get("previously_used", False),
            }
        )
        if len(numbered) >= 80:
            break
    return numbered


def candidates_for_prompt(candidates: list[dict]) -> list[dict]:
    return [
        {
            "ref": item["ref"],
            "source_name": item["source_name"],
            "topic_candidates": item["topic_candidates"],
            "geography": item["geography"],
            "title": item["title"],
            "published_at": item["published_at"],
            "summary": item["summary"],
            "candidate_date": item.get("candidate_date"),
            "fallback_age_days": item.get("fallback_age_days", 0),
        }
        for item in candidates
    ]


def candidates_for_section(candidates: list[dict], topic: str) -> list[dict]:
    topic_candidates = [candidate for candidate in candidates if topic in candidate.get("topic_candidates", [])]
    return sorted(topic_candidates, key=lambda candidate: candidate_priority(candidate, topic))


def log_quality_rejection(rejections: list[dict], topic: str, reason: str, **details: object) -> None:
    entry = {"topic": topic, "reason": reason, **details}
    rejections.append(entry)
    print(json.dumps({"quality_gate_rejection": entry}, ensure_ascii=False), file=sys.stderr)


def rejection_feedback(topic_id: str, rejections: list[dict], limit: int = 8) -> list[dict]:
    topic_rejections = [item for item in rejections if item.get("topic") == topic_id]
    return topic_rejections[-limit:]


def call_deepseek_for_sections(
    candidates: list[dict],
    site: dict,
    date_text: str,
    rejections: list[dict] | None = None,
    target_topics: set[str] | None = None,
    feedback_by_topic: dict[str, list[dict]] | None = None,
) -> list[dict]:
    if rejections is None:
        rejections = []
    if feedback_by_topic is None:
        feedback_by_topic = {}
    api_key = os.environ["DEEPSEEK_API_KEY"]
    model = os.getenv("LLM_PRIMARY_MODEL", "deepseek-chat")
    numbered_candidates = number_candidates(candidates, site)
    reports = []

    for topic in site["topics"]:
        topic_id = topic["id"]
        if target_topics is not None and topic_id not in target_topics:
            continue
        topic_candidates = candidates_for_section(numbered_candidates, topic_id)
        if len(topic_candidates) < MIN_NEWS_ITEMS_PER_SECTION:
            log_quality_rejection(
                rejections,
                topic_id,
                "insufficient_candidate_material",
                candidate_count=len(topic_candidates),
                required_candidates=MIN_NEWS_ITEMS_PER_SECTION,
            )
            continue

        accepted_reports: list[dict] = []
        attempt_feedback = list(feedback_by_topic.get(topic_id, []))
        for attempt in range(1, SECTION_GENERATION_ATTEMPTS + 1):
            prompt = {
                "date": date_text,
                "audience": "ordinary Canadians; Vancouver local readers have a dedicated section",
                "target_section": topic,
                "section_rules": {
                    "min_news_items": MIN_NEWS_ITEMS_PER_SECTION,
                    "max_news_items": MAX_NEWS_ITEMS_PER_SECTION,
                    "fallback_window": f"Use publication-date news first. If there is not enough strong material, use unused candidates from the previous {FALLBACK_LOOKBACK_DAYS} days.",
                    "finance_priority": "For finance, prioritize Bank of Canada, Federal Reserve, interest rates, inflation/CPI, jobs, unemployment, mortgages, rent, and household cash flow.",
                    "repair_requirement": "If this is a retry, fix the listed quality failures instead of returning a weak or empty section.",
                },
                "previous_quality_failures_to_fix": attempt_feedback + rejection_feedback(topic_id, rejections),
                "editorial_rules": site["editorial_rules"],
                "source_rules": [
                    "Sources are identified only by refs like [S1]. You must not output URLs.",
                    "Every specific claim about money, dates, eligibility, deadlines, quotes, or opposing views must cite one or more source refs.",
                    "Use only source_refs that appear in the provided candidates.",
                    "Write only the requested target_section. Do not return other sections.",
                    "The returned section.topic must repeat target_section.id exactly in English. Do not translate the topic id.",
                ],
                "writing_template": {
                    "section_overview": "今天有 X 条新闻会对我们的生活造成影响。",
                    "section_composition": "Return 1-3 distinct, evidence-backed news analyses. At least one accepted article is required for publication.",
                    "per_news_item": [
                        "A clear, forceful title that directly names the core issue or viewpoint.",
                        "Write 800-1500 Chinese characters across body_markdown and impact_markdown.",
                        "Use full readable Chinese paragraphs, never bullet points.",
                        "Start by saying what happened in one sentence and directly explain what it means.",
                        "Use 0-2 optional Markdown ## subheadings only for genuinely distinct questions.",
                        "The final paragraph of body_markdown must begin with 最后，总的来说，.",
                        "Put the impact block in impact_markdown only, exactly once, and nowhere in body_markdown.",
                        "impact_markdown must start with 这个对于[具体群体]的影响是： and contain [短期], [中期], and [长期] in that order.",
                        "Nothing may follow the impact block except source refs attached to its claims.",
                        "Do not use em dashes, Chinese dash punctuation, bullet points, or URLs.",
                    ],
                },
                "candidates": candidates_for_prompt(topic_candidates),
                "required_json_shape": {
                    "section": {
                        "topic": topic_id,
                        "overview": "今天有 X 条新闻会对我们的生活造成影响。",
                        "news_items": [
                            {
                                "title": "clear Chinese headline",
                                "summary": "one sentence",
                                "body_markdown": "800-1500-character article analysis with [S1] refs and 0-2 optional ## headings",
                                "impact_markdown": "这个对于[具体群体]的影响是： followed by [短期] / [中期] / [长期]",
                                "source_refs": ["S1", "S2"],
                            }
                        ],
                    }
                },
            }
            response = requests.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                NEWS_ANALYST_SYSTEM_PROMPT
                                + "\n\n硬性技术规则：Return strict JSON only for the requested section. Never output source URLs. "
                                "Only cite source refs like [S1]. Return 1-3 complete articles. "
                                "At least one article must pass the quality gates. "
                                "Each article must be 800-1500 Chinese characters. Do not write bullet points. "
                                "The fixed impact block belongs once at the very end of each article. "
                                "If prior quality failures are supplied, rewrite to fix them."
                            ),
                        },
                        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                    ],
                    "temperature": 0.2 if attempt == 1 else 0.1,
                    "max_tokens": 8192,
                    "response_format": {"type": "json_object"},
                },
                timeout=120,
            )
            response.raise_for_status()
            choice = response.json()["choices"][0]
            finish_reason = choice.get("finish_reason")
            if finish_reason == "length":
                raise RuntimeError(
                    f"DeepSeek output for section {topic_id} was truncated because finish_reason=length; "
                    "the JSON response was not parsed."
                )
            if finish_reason not in (None, "stop"):
                raise RuntimeError(f"DeepSeek stopped unexpectedly for section {topic_id}: finish_reason={finish_reason}")

            text = choice["message"]["content"]
            try:
                payload = json.loads(extract_json(text))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"DeepSeek returned invalid JSON for section {topic_id} after {len(text)} characters: {exc}"
                ) from exc

            raw_section = payload.get("section")
            raw_sections = payload.get("sections", []) if raw_section is None else [raw_section]
            if not raw_sections:
                log_quality_rejection(
                    rejections,
                    topic_id,
                    "model_returned_no_section",
                    candidate_count=len(topic_candidates),
                    attempt=attempt,
                )
                attempt_feedback = rejection_feedback(topic_id, rejections)
                continue
            matching_sections = []
            for returned_section in raw_sections:
                returned_topic = str(returned_section.get("topic") or "").strip()
                normalized_topic = normalize_topic_id(returned_topic, site)
                if normalized_topic != topic_id:
                    log_quality_rejection(
                        rejections,
                        topic_id,
                        "model_returned_wrong_section",
                        returned_topic=returned_topic,
                        attempt=attempt,
                    )
                    continue
                normalized_section = dict(returned_section)
                normalized_section["topic"] = topic_id
                matching_sections.append(normalized_section)
            if not matching_sections:
                attempt_feedback = rejection_feedback(topic_id, rejections)
                continue
            before_count = len(rejections)
            accepted_reports = normalize_section_reports(
                matching_sections,
                numbered_candidates,
                site,
                date_text,
                rejections=rejections,
            )
            if accepted_reports:
                break
            attempt_feedback = rejections[before_count:] or rejection_feedback(topic_id, rejections)

        reports.extend(accepted_reports)

    return reports

def extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return text


def normalize_source_refs(source_refs: object, text: str, source_by_ref: dict[str, dict]) -> list[str]:
    refs = []
    if isinstance(source_refs, list):
        for raw in source_refs:
            ref = str(raw).strip().strip("[]")
            if re.fullmatch(r"S\d+", ref):
                refs.append(ref)
    refs.extend(f"S{match}" for match in SOURCE_REF_RE.findall(text))
    unique_refs = []
    for ref in refs:
        if ref in source_by_ref and ref not in unique_refs:
            unique_refs.append(ref)
    return unique_refs


def has_bullet_markers(markdown: str) -> bool:
    return bool(BULLET_RE.search(markdown))


def has_dash_punctuation(markdown: str) -> bool:
    return bool(DASH_RE.search(markdown))


def has_source_url(markdown: str) -> bool:
    return bool(URL_RE.search(markdown))


def analysis_char_count(*parts: str) -> int:
    text = "\n".join(parts)
    text = re.sub(r"\[S\d+\]", "", text)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"\s+", "", text)
    return len(text)


def heading_count(markdown: str) -> int:
    return len(re.findall(r"^#{2,3}\s+", markdown, flags=re.MULTILINE))


def limit_headings(markdown: str, max_headings: int = MAX_HEADINGS_PER_ITEM) -> str:
    kept = 0
    lines = []
    for line in markdown.splitlines():
        if re.match(r"^#{2,3}\s+", line):
            kept += 1
            if kept > max_headings:
                line = re.sub(r"^#{2,3}\s+", "", line).strip()
        lines.append(line)
    return "\n".join(lines).strip()


def has_required_conclusion(body_markdown: str) -> bool:
    paragraphs = [block.strip() for block in re.split(r"\n\s*\n", body_markdown) if block.strip()]
    if not paragraphs:
        return False
    final_paragraph = re.sub(r"^#{2,3}\s+", "", paragraphs[-1]).strip()
    return final_paragraph.startswith("最后，总的来说，")


def is_valid_impact_text(impact_markdown: str) -> bool:
    matches = list(IMPACT_PREFIX_RE.finditer(impact_markdown))
    if len(matches) != 1 or impact_markdown[: matches[0].start()].strip():
        return False
    short_pos = impact_markdown.find("[短期]", matches[0].end())
    middle_pos = impact_markdown.find("[中期]", short_pos + 1)
    long_pos = impact_markdown.find("[长期]", middle_pos + 1)
    return short_pos >= 0 and short_pos < middle_pos < long_pos


def move_trailing_impact_block(body_markdown: str, impact_markdown: str) -> tuple[str, str, bool]:
    matches = list(IMPACT_PREFIX_RE.finditer(body_markdown))
    if len(matches) != 1:
        return body_markdown, impact_markdown, False
    start = matches[0].start()
    trailing_block = body_markdown[start:].strip()
    if not is_valid_impact_text(trailing_block):
        return body_markdown, impact_markdown, False

    long_pos = trailing_block.find("[长期]")
    long_term_tail = trailing_block[long_pos + len("[长期]") :].strip()
    tail_paragraphs = [part for part in re.split(r"\n\s*\n", long_term_tail) if part.strip()]
    if len(tail_paragraphs) > 1:
        return body_markdown, impact_markdown, False

    corrected_body = body_markdown[:start].rstrip()
    corrected_impact = impact_markdown if is_valid_impact_text(impact_markdown) else trailing_block
    if not corrected_body or not is_valid_impact_text(corrected_impact):
        return body_markdown, impact_markdown, False
    return corrected_body, corrected_impact, True


def has_valid_impact_block(body_markdown: str, impact_markdown: str) -> bool:
    return not IMPACT_PREFIX_RE.search(body_markdown) and is_valid_impact_text(impact_markdown)


def short_summary(text: str) -> str:
    plain = re.sub(r"\[S\d+\]", "", text)
    plain = re.sub(r"#+\s*", "", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    if "。" in plain:
        return plain.split("。", 1)[0][:120] + "。"
    return plain[:120]


def citations_for_refs(refs: list[str], source_by_ref: dict[str, dict]) -> list[dict]:
    citations = []
    for ref in refs:
        source = source_by_ref[ref]
        citations.append(
            {
                "ref": ref,
                "title": source.get("title") or source.get("source_name") or ref,
                "url": source["url"],
                "source_name": source.get("source_name", ""),
            }
        )
    return citations


def normalize_section_reports(
    raw_sections: list[dict],
    sources: list[dict],
    site: dict,
    date_text: str,
    rejections: list[dict] | None = None,
) -> list[dict]:
    if rejections is None:
        rejections = []
    source_by_ref = {source["ref"]: source for source in sources}
    allowed_topics = topic_ids(site)
    reports = []

    for raw_section in raw_sections:
        raw_topic = str(raw_section.get("topic") or "").strip()
        topic = normalize_topic_id(raw_topic, site)
        if topic not in allowed_topics:
            log_quality_rejection(rejections, raw_topic or "unknown", "invalid_topic")
            continue

        news_items = []
        raw_items = raw_section.get("news_items") or []
        for item_index, raw_item in enumerate(raw_items, start=1):
            title = str(raw_item.get("title") or "").strip()
            body_markdown = limit_headings(str(raw_item.get("body_markdown") or "").strip())
            impact_markdown = str(raw_item.get("impact_markdown") or "").strip()
            rejection_details = {"item_index": item_index, "title": title[:120]}
            body_markdown, impact_markdown, impact_was_moved = move_trailing_impact_block(
                body_markdown, impact_markdown
            )
            if impact_was_moved:
                print(
                    json.dumps(
                        {
                            "quality_gate_correction": {
                                "topic": topic,
                                "correction": "moved_trailing_impact_block_to_impact_markdown",
                                **rejection_details,
                            }
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
            if not title or not body_markdown or not impact_markdown:
                log_quality_rejection(rejections, topic, "missing_required_article_field", **rejection_details)
                continue
            if has_bullet_markers(body_markdown) or has_bullet_markers(impact_markdown):
                log_quality_rejection(rejections, topic, "bullet_points_not_allowed", **rejection_details)
                continue
            combined_article = "\n".join([title, body_markdown, impact_markdown])
            if has_dash_punctuation(combined_article):
                log_quality_rejection(rejections, topic, "dash_punctuation_not_allowed", **rejection_details)
                continue
            item_heading_count = heading_count(body_markdown)
            item_char_count = analysis_char_count(body_markdown, impact_markdown)
            if item_char_count < MIN_NEWS_CHARS or item_char_count > MAX_NEWS_CHARS:
                log_quality_rejection(
                    rejections,
                    topic,
                    "article_length_out_of_range",
                    actual_chars=item_char_count,
                    min_chars=MIN_NEWS_CHARS,
                    max_chars=MAX_NEWS_CHARS,
                    **rejection_details,
                )
                continue
            if not has_valid_impact_block(body_markdown, impact_markdown):
                log_quality_rejection(rejections, topic, "invalid_or_misplaced_impact_block", **rejection_details)
                continue
            if not has_required_conclusion(body_markdown):
                log_quality_rejection(rejections, topic, "missing_final_conclusion", **rejection_details)
                continue
            combined_text = "\n".join(
                [title, str(raw_item.get("summary") or ""), body_markdown, impact_markdown]
            )
            if has_source_url(combined_text):
                log_quality_rejection(rejections, topic, "model_output_url_not_allowed", **rejection_details)
                continue

            refs = normalize_source_refs(raw_item.get("source_refs"), combined_text, source_by_ref)
            if not refs:
                log_quality_rejection(rejections, topic, "missing_valid_source_refs", **rejection_details)
                continue

            tts_text = "\n\n".join([title, body_markdown, impact_markdown])

            news_items.append(
                {
                    "id": make_slug(title, date_text).split("/")[-1],
                    "title": title,
                    "summary": str(raw_item.get("summary") or "").strip() or short_summary(body_markdown),
                    "body_markdown": body_markdown,
                    "body_html": simple_markdown_to_html(body_markdown),
                    "impact_markdown": impact_markdown,
                    "impact_html": simple_markdown_to_html(impact_markdown),
                    "source_refs": refs,
                    "citations": citations_for_refs(refs, source_by_ref),
                    "tts_text": tts_text,
                    "analysis_chars": item_char_count,
                    "heading_count": item_heading_count,
                    "audio_path": "",
                }
            )

        if len(news_items) < MIN_NEWS_ITEMS_PER_SECTION:
            log_quality_rejection(
                rejections,
                topic,
                "section_has_too_few_accepted_articles",
                candidate_articles=len(raw_items),
                accepted_articles=len(news_items),
                required_articles=MIN_NEWS_ITEMS_PER_SECTION,
            )
            continue
        news_items = news_items[:MAX_NEWS_ITEMS_PER_SECTION]

        reports.append(
            {
                "topic": topic,
                "topic_name": topic_name(topic, site["topics"]),
                "topic_description": topic_description(topic, site["topics"]),
                "publication_date": date_text,
                "overview": f"今天有 {len(news_items)} 条新闻会对我们的生活造成影响。",
                "news_items": news_items,
                "audio_path": "",
            }
        )

    return reports


def total_news_count(sections: list[dict]) -> int:
    return sum(len(section.get("news_items", [])) for section in sections)


def citation_urls(item: dict) -> set[str]:
    return {
        str(citation.get("url") or "").strip()
        for citation in item.get("citations", [])
        if str(citation.get("url") or "").startswith("http")
    }


def annotate_cross_section_duplicates(sections: list[dict], site: dict) -> None:
    occurrences: dict[str, list[tuple[dict, dict]]] = {}
    for section in sections:
        for item in section.get("news_items", []):
            for url in citation_urls(item):
                occurrences.setdefault(url, []).append((section, item))

    topic_order = {topic["id"]: index for index, topic in enumerate(site.get("topics", []))}
    for section in sections:
        for item in section.get("news_items", []):
            related: dict[tuple[str, str], dict] = {}
            for url in citation_urls(item):
                for other_section, other_item in occurrences.get(url, []):
                    if other_section.get("topic") == section.get("topic"):
                        continue
                    key = (other_section.get("topic", ""), other_item.get("id", ""))
                    related[key] = {
                        "topic": other_section.get("topic", ""),
                        "topic_name": other_section.get("topic_name") or topic_name(other_section.get("topic", ""), site["topics"]),
                        "title": other_item.get("title", ""),
                        "href": f"/topics/{other_section.get('topic')}#{other_item.get('id')}",
                    }

            links = sorted(
                related.values(),
                key=lambda link: (topic_order.get(link["topic"], 999), link["title"]),
            )
            if not links:
                item.pop("cross_section_notice", None)
                item.pop("cross_section_links", None)
                continue

            topic_names = []
            for link in links:
                if link["topic_name"] not in topic_names:
                    topic_names.append(link["topic_name"])
            notice = f"这条新闻的影响面很广，我们在{'、'.join(topic_names)}板块也有分析。你可以跳过去看另一角度。"
            item["cross_section_notice"] = notice
            item["cross_section_links"] = links
            tts_text = str(item.get("tts_text") or "").strip()
            if tts_text and not tts_text.startswith(notice):
                item["tts_text"] = f"{notice}\n\n{tts_text}"


def merge_sections(existing: list[dict], updates: list[dict]) -> list[dict]:
    merged = {section["topic"]: section for section in existing}
    for section in updates:
        merged[section["topic"]] = section
    return list(merged.values())


def missing_section_topics(site: dict, sections: list[dict]) -> set[str]:
    present = {
        section["topic"]
        for section in sections
        if len(section.get("news_items", [])) >= MIN_NEWS_ITEMS_PER_SECTION
    }
    return {topic["id"] for topic in site.get("topics", [])} - present


def feedback_by_topic_from_rejections(rejections: list[dict], topics: set[str]) -> dict[str, list[dict]]:
    return {
        topic: [item for item in rejections if item.get("topic") == topic][-10:]
        for topic in topics
    }


def log_gemini_review_unavailable(rejections: list[dict], sections: list[dict], exc: Exception) -> None:
    details = [str(exc)]
    for section in sections:
        log_quality_rejection(
            rejections,
            section.get("topic", "unknown"),
            "gemini_review_unavailable",
            articles_kept=len(section.get("news_items", [])),
            details=details,
        )


def has_sentence_ending(text: str) -> bool:
    return bool(re.search(r"[。！？!?；;.]$", text.rstrip()))


def sentence_safe_piece(text: str) -> str:
    piece = text.strip()
    if piece and not has_sentence_ending(piece):
        piece += "。"
    return piece


def split_oversized_text(text: str, max_bytes: int) -> list[str]:
    pieces = []
    remaining = text
    while remaining:
        encoded = remaining.encode("utf-8")
        if len(encoded) <= max_bytes:
            pieces.append(sentence_safe_piece(remaining))
            break
        cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
        preferred = max(cut.rfind(mark) for mark in ("，", "、", ",", " ", "：", ":"))
        if preferred > 0:
            cut = cut[: preferred + 1]
        cut = cut.rstrip()
        if not cut:
            cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
        pieces.append(sentence_safe_piece(cut))
        remaining = remaining[len(cut) :].lstrip()
    return pieces


def split_tts_text(
    text: str,
    max_bytes: int = TTS_CHUNK_MAX_BYTES,
    max_sentence_bytes: int = TTS_SENTENCE_MAX_BYTES,
) -> list[str]:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if max_sentence_bytes <= 0:
        raise ValueError("max_sentence_bytes must be positive")
    max_sentence_bytes = min(max_sentence_bytes, max_bytes)
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    units = []
    for paragraph in paragraphs:
        sentences = [part.strip() for part in re.split(r"(?<=[。！？!?；;])", paragraph) if part.strip()]
        if not sentences:
            sentences = [paragraph]
        for sentence in sentences:
            if len(sentence.encode("utf-8")) <= max_sentence_bytes:
                units.append(sentence)
            else:
                units.extend(split_oversized_text(sentence, max_sentence_bytes))

    chunks = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate.encode("utf-8")) <= max_bytes:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    return chunks


def speech_text(markdown: str) -> str:
    text = re.sub(r"\[S\d+\]", "", markdown)
    text = re.sub(r"^#{2,3}\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"[ \t]+", " ", text).strip()


def mp3_duration_seconds(data: bytes) -> float:
    bitrate_v1_l3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    bitrate_v2_l3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
    sample_rates = [44100, 48000, 32000]
    duration = 0.0
    index = 0
    while index + 4 <= len(data):
        if data[index : index + 3] == b"ID3" and index + 10 <= len(data):
            size_bytes = data[index + 6 : index + 10]
            tag_size = sum((byte & 0x7F) << shift for byte, shift in zip(size_bytes, (21, 14, 7, 0)))
            index += 10 + tag_size
            continue
        header = int.from_bytes(data[index : index + 4], "big")
        if (header >> 21) & 0x7FF != 0x7FF:
            index += 1
            continue
        version_id = (header >> 19) & 0x3
        layer_id = (header >> 17) & 0x3
        bitrate_index = (header >> 12) & 0xF
        sample_index = (header >> 10) & 0x3
        padding = (header >> 9) & 0x1
        if version_id == 1 or layer_id != 1 or bitrate_index in (0, 15) or sample_index == 3:
            index += 1
            continue
        sample_rate = sample_rates[sample_index]
        if version_id == 2:
            sample_rate //= 2
        elif version_id == 0:
            sample_rate //= 4
        bitrate = (bitrate_v1_l3 if version_id == 3 else bitrate_v2_l3)[bitrate_index]
        samples_per_frame = 1152 if version_id == 3 else 576
        frame_length = ((144 if version_id == 3 else 72) * bitrate * 1000 // sample_rate) + padding
        if frame_length <= 4 or index + frame_length > len(data):
            index += 1
            continue
        duration += samples_per_frame / sample_rate
        index += frame_length
    return duration


def concatenate_mp3_segments(segments: list[bytes]) -> tuple[bytes, str]:
    combined = b"".join(segments)
    segment_durations = [mp3_duration_seconds(segment) for segment in segments]
    combined_duration = mp3_duration_seconds(combined)
    expected_duration = sum(segment_durations)
    direct_concat_valid = (
        bool(segments)
        and all(duration > 0 for duration in segment_durations)
        and abs(combined_duration - expected_duration) <= max(0.25, expected_duration * 0.02)
    )
    if len(segments) <= 1 or direct_concat_valid:
        return combined, "byte_concat"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return combined, "byte_concat_unverified"
    with tempfile.TemporaryDirectory(prefix="infogap-tts-") as temp_name:
        temp_dir = Path(temp_name)
        concat_lines = []
        for index, segment in enumerate(segments):
            segment_path = temp_dir / f"segment-{index:03d}.mp3"
            segment_path.write_bytes(segment)
            concat_lines.append(f"file '{segment_path.name}'")
        (temp_dir / "concat.txt").write_text("\n".join(concat_lines), encoding="utf-8")
        output_path = temp_dir / "combined.mp3"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                "concat.txt",
                "-c",
                "copy",
                str(output_path),
            ],
            cwd=temp_dir,
            check=True,
        )
        return output_path.read_bytes(), "ffmpeg_concat"


def retry_delay_seconds(response: requests.Response | None, attempt: int) -> float:
    retry_after = response.headers.get("retry-after") if response is not None else None
    if retry_after:
        try:
            return min(float(retry_after), 45.0)
        except ValueError:
            pass
    return min(10.0 * (2 ** (attempt - 1)), 45.0)


def post_gemini_review(url: str, api_key: str, payload: dict) -> requests.Response:
    last_error = ""
    for attempt in range(1, GEMINI_REVIEW_MAX_ATTEMPTS + 1):
        response = None
        try:
            response = requests.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=60,
            )
            if response.status_code not in GEMINI_REVIEW_RETRY_STATUSES:
                response.raise_for_status()
                return response
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            if "prepayment credits are depleted" in response.text.lower():
                break
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code not in GEMINI_REVIEW_RETRY_STATUSES:
                raise
            response = exc.response
            last_error = f"HTTP {status_code}: {str(exc)[:300]}"
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:300]}"

        if attempt >= GEMINI_REVIEW_MAX_ATTEMPTS:
            break
        delay = retry_delay_seconds(response, attempt)
        print(
            json.dumps(
                {
                    "gemini_review_retry": {
                        "attempt": attempt,
                        "max_attempts": GEMINI_REVIEW_MAX_ATTEMPTS,
                        "delay_seconds": delay,
                        "error": last_error,
                    }
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        time.sleep(delay)

    raise GeminiReviewUnavailable(
        f"Gemini review unavailable after {GEMINI_REVIEW_MAX_ATTEMPTS} attempts: {last_error}"
    )


def retryable_google_api_errors(google_exceptions) -> tuple[type[BaseException], ...]:
    names = (
        "ServiceUnavailable",
        "DeadlineExceeded",
        "TooManyRequests",
        "ResourceExhausted",
        "InternalServerError",
        "BadGateway",
        "GatewayTimeout",
    )
    return tuple(
        error_type
        for name in names
        if isinstance((error_type := getattr(google_exceptions, name, None)), type)
    )


def skippable_google_tts_errors(google_exceptions) -> tuple[type[BaseException], ...]:
    invalid_argument = getattr(google_exceptions, "InvalidArgument", None)
    retryable = retryable_google_api_errors(google_exceptions)
    if isinstance(invalid_argument, type):
        return retryable + (invalid_argument,)
    return retryable


def tts_retry_delay_seconds(attempt: int) -> float:
    return min(5.0 * (2 ** (attempt - 1)), 30.0)


def synthesize_speech_chunk_with_retry(
    client,
    texttospeech,
    google_exceptions,
    text_chunk: str,
    voice_name: str,
    topic: str,
    article_id: str,
    chunk_index: int,
):
    retryable_errors = retryable_google_api_errors(google_exceptions)
    for attempt in range(1, TTS_MAX_ATTEMPTS + 1):
        try:
            return client.synthesize_speech(
                input=texttospeech.SynthesisInput(text=text_chunk),
                voice=texttospeech.VoiceSelectionParams(language_code="cmn-CN", name=voice_name),
                audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3),
            )
        except retryable_errors as exc:
            if attempt >= TTS_MAX_ATTEMPTS:
                raise
            delay = tts_retry_delay_seconds(attempt)
            print(
                json.dumps(
                    {
                        "tts_retry": {
                            "topic": topic,
                            "article_id": article_id,
                            "chunk_index": chunk_index,
                            "attempt": attempt,
                            "max_attempts": TTS_MAX_ATTEMPTS,
                            "delay_seconds": delay,
                            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        }
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            time.sleep(delay)


def review_with_gemini(
    sections: list[dict],
    candidates: list[dict],
    site: dict,
    rejections: list[dict] | None = None,
) -> list[dict]:
    if rejections is None:
        rejections = []
    if not sections:
        return []
    api_key = os.environ["GEMINI_API_KEY"]
    model = os.getenv("LLM_REVIEW_MODEL", "gemini-3.5-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    source_by_ref = {source["ref"]: source for source in number_candidates(candidates, site)}
    prompt = {
        "task": (
            "Review each article only for factual evidence quality. For every article decide whether the cited evidence "
            "actually supports the nearby claims, whether any number, amount, date, deadline, or eligibility condition "
            "is absent from the supplied evidence, and whether any conclusion makes an unsupported leap beyond the facts. "
            "Return one review result for every article. Do not assess writing style or presentation."
        ),
        "articles": [
            {
                "topic": section["topic"],
                "article_id": item["id"],
                "title": item["title"],
                "body_markdown": item["body_markdown"],
                "impact_markdown": item["impact_markdown"],
                "evidence": [
                    {
                        "ref": ref,
                        "source_name": source_by_ref[ref].get("source_name", ""),
                        "title": source_by_ref[ref].get("title", ""),
                        "published_at": source_by_ref[ref].get("published_at"),
                        "summary": source_by_ref[ref].get("summary", ""),
                    }
                    for ref in item["source_refs"]
                    if ref in source_by_ref
                ],
            }
            for section in sections
            for item in section["news_items"]
        ],
        "required_json_shape": {
            "reviews": [
                {
                    "topic": "exact topic id from the article",
                    "article_id": "exact article_id from the article",
                    "ok": True,
                    "reasons": ["specific evidence problem when ok is false"],
                }
            ]
        },
    }
    response = post_gemini_review(
        url,
        api_key,
        {
            "contents": [{"parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
    )
    response_payload = response.json()
    candidate = response_payload["candidates"][0]
    finish_reason = candidate.get("finishReason")
    if finish_reason not in (None, "STOP"):
        raise RuntimeError(f"Gemini review stopped unexpectedly: finishReason={finish_reason}")
    text = "".join(part.get("text", "") for part in candidate["content"]["parts"])
    try:
        review_payload = json.loads(extract_json(text))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned invalid review JSON: {exc}") from exc

    review_by_article = {}
    for review in review_payload.get("reviews", []):
        topic = normalize_topic_id(review.get("topic"), site)
        article_id = str(review.get("article_id") or "").strip()
        if topic and article_id:
            review_by_article[(topic, article_id)] = review

    expected_keys = {
        (section["topic"], item["id"])
        for section in sections
        for item in section["news_items"]
    }
    missing_keys = expected_keys - set(review_by_article)
    if missing_keys:
        raise RuntimeError(f"Gemini review omitted {len(missing_keys)} article(s): {sorted(missing_keys)}")

    accepted_sections = []
    for section in sections:
        accepted_items = []
        for item in section["news_items"]:
            review = review_by_article[(section["topic"], item["id"])]
            if review.get("ok") is True:
                accepted_items.append(item)
                continue
            reasons = review.get("reasons") or review.get("reason") or []
            if isinstance(reasons, str):
                reasons = [reasons]
            log_quality_rejection(
                rejections,
                section["topic"],
                "gemini_evidence_review_rejected",
                article_id=item["id"],
                title=item["title"][:120],
                details=[str(reason) for reason in reasons],
            )

        if len(accepted_items) < MIN_NEWS_ITEMS_PER_SECTION:
            log_quality_rejection(
                rejections,
                section["topic"],
                "section_has_too_few_articles_after_gemini",
                articles_before_review=len(section["news_items"]),
                accepted_articles=len(accepted_items),
                required_articles=MIN_NEWS_ITEMS_PER_SECTION,
            )
            continue
        accepted_section = dict(section)
        accepted_section["news_items"] = accepted_items[:MAX_NEWS_ITEMS_PER_SECTION]
        accepted_section["overview"] = f"今天有 {len(accepted_section['news_items'])} 条新闻会对我们的生活造成影响。"
        accepted_sections.append(accepted_section)

    return accepted_sections


def synthesize_tts(sections: list[dict], date_text: str) -> None:
    if not sections:
        return
    raw_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if raw_credentials:
        path = Path(os.getenv("RUNNER_TEMP", str(ROOT))) / "google-tts-credentials.json"
        if raw_credentials.strip().startswith("{"):
            path.write_text(raw_credentials, encoding="utf-8")
        else:
            path.write_bytes(base64.b64decode(raw_credentials))
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)

    from google.api_core import exceptions as google_exceptions
    from google.cloud import texttospeech

    client = texttospeech.TextToSpeechClient()
    voice_name = os.getenv("TTS_VOICE", "cmn-CN-Chirp3-HD-Achernar")
    audio_dir = GENERATED / "audio" / date_text
    audio_dir.mkdir(parents=True, exist_ok=True)

    for section in sections:
        if not section.get("news_items"):
            continue
        for item_index, item in enumerate(section["news_items"], start=1):
            filename = f"{section['topic']}-{item_index:02d}-{item['id']}.mp3"
            output = audio_dir / filename
            spoken_text = speech_text(item["tts_text"])
            text_chunks = split_tts_text(spoken_text)
            if not text_chunks:
                raise RuntimeError(f"TTS text is empty for article {item['id']} in section {section['topic']}")
            audio_segments = []
            try:
                for chunk_index, text_chunk in enumerate(text_chunks, start=1):
                    response = synthesize_speech_chunk_with_retry(
                        client,
                        texttospeech,
                        google_exceptions,
                        text_chunk,
                        voice_name,
                        section["topic"],
                        item["id"],
                        chunk_index,
                    )
                    audio_segments.append(response.audio_content)
            except skippable_google_tts_errors(google_exceptions) as exc:
                item["audio_path"] = ""
                item["audio_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                print(
                    json.dumps(
                        {
                            "tts_error": {
                                "topic": section["topic"],
                                "article_id": item["id"],
                                "segments": len(text_chunks),
                                "error": item["audio_error"],
                            }
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                continue
            combined_audio, merge_method = concatenate_mp3_segments(audio_segments)
            output.write_bytes(combined_audio)
            duration_seconds = round(mp3_duration_seconds(combined_audio), 2)
            item["audio_path"] = f"audio/{date_text}/{filename}"
            item["audio_segments"] = len(text_chunks)
            item["audio_duration_seconds"] = duration_seconds
            print(
                json.dumps(
                    {
                        "tts": {
                            "topic": section["topic"],
                            "article_id": item["id"],
                            "segments": len(text_chunks),
                            "duration_seconds": duration_seconds,
                            "audio_bytes": len(combined_audio),
                            "merge_method": merge_method,
                        }
                    },
                    ensure_ascii=False,
                )
            )


def sections_for_render(site: dict, sections: list[dict], publication_date: str) -> list[dict]:
    by_topic = {section["topic"]: section for section in sections}
    rendered = []
    for topic in site.get("topics", []):
        report = by_topic.get(topic["id"])
        if report and len(report.get("news_items", [])) >= MIN_NEWS_ITEMS_PER_SECTION:
            rendered.append(report)
        else:
            rendered.append(
                {
                    "topic": topic["id"],
                    "topic_name": topic["name"],
                    "topic_description": topic.get("description", ""),
                    "publication_date": publication_date,
                    "overview": "今天这个主题还没有通过事实核查的分析。",
                    "news_items": [],
                    "audio_path": "",
                }
            )
    return rendered


def render_site(site: dict, sections: list[dict], publication_date: str, preserve_audio: bool = False) -> None:
    if GENERATED.exists():
        for child in GENERATED.iterdir():
            if preserve_audio and child.name == "audio":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        GENERATED.mkdir(parents=True)

    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )

    rendered_sections = sections_for_render(site, sections, publication_date)
    total_count = total_news_count(rendered_sections)
    shutil.copyfile(ROOT / "templates" / "base.css", GENERATED / "base.css")

    (GENERATED / "index.html").write_text(
        env.get_template("index.html.j2").render(
            site=site,
            sections=rendered_sections,
            publication_date=publication_date,
            total_news=total_count,
        ),
        encoding="utf-8",
    )

    (GENERATED / "privacy.html").write_text(
        env.get_template("privacy.html.j2").render(site=site),
        encoding="utf-8",
    )

    topics_dir = GENERATED / "topics"
    topics_dir.mkdir()
    for section in rendered_sections:
        (topics_dir / f"{section['topic']}.html").write_text(
            env.get_template("topic.html.j2").render(site=site, section=section),
            encoding="utf-8",
        )

    data_dir = GENERATED / "data"
    data_dir.mkdir()
    (data_dir / "index.json").write_text(
        json.dumps({"publication_date": publication_date, "sections": rendered_sections}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_run_artifacts(
    date_text: str,
    candidates: list[dict],
    sections: list[dict],
    site: dict,
    quality_rejections: list[dict] | None = None,
) -> None:
    run_dir = RUNS / date_text
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "sections.json").write_text(json.dumps(sections, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "source-candidates.json").write_text(
        json.dumps(number_candidates(candidates, site), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "quality-rejections.json").write_text(
        json.dumps(quality_rejections or [], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--review", action="store_true")
    parser.add_argument("--tts", action="store_true")
    parser.add_argument("--validate-source", action="append", default=[])
    args = parser.parse_args()
    publication_date = normalize_date_text(args.date)

    site = load_json(ROOT / "config" / "site.json")
    source_registry = load_json(ROOT / "config" / "source-registry.json")
    if args.validate_source:
        requested_source_ids = set(args.validate_source)
        collection_registry = {
            **source_registry,
            "sources": [
                source for source in source_registry.get("sources", []) if source.get("id") in requested_source_ids
            ],
        }
        candidates = collect_candidates(collection_registry, publication_date, site["timezone"])
        debug_by_id = {item["id"]: item for item in collect_candidates.last_debug}
        requested_results = [
            debug_by_id.get(source_id, {"id": source_id, "entries": 0, "date_matches": 0, "error": "source_not_enabled_or_missing"})
            for source_id in args.validate_source
        ]
        print(json.dumps({"source_debug": requested_results}, ensure_ascii=False, indent=2))
        failed_sources = [
            item["id"] for item in requested_results if item.get("error") or int(item.get("entries") or 0) <= 0
        ]
        if failed_sources:
            print(
                json.dumps(
                    {"ok": False, "error": "Source validation failed.", "failed_sources": failed_sources},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1
        print(json.dumps({"ok": True, "validated_sources": args.validate_source}, ensure_ascii=False))
        return 0

    candidates = collect_candidates_for_publication(source_registry, publication_date, site["timezone"])

    if not candidates and not args.allow_empty:
        print("No enabled source produced candidates. Enable verified feeds before the real run.", file=sys.stderr)
        print(json.dumps({"source_debug": collect_candidates.last_debug}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    quality_rejections = []
    if args.use_llm:
        sections = call_deepseek_for_sections(candidates, site, publication_date, rejections=quality_rejections)
        missing_topics = missing_section_topics(site, sections)
        if missing_topics:
            repair_sections = call_deepseek_for_sections(
                candidates,
                site,
                publication_date,
                rejections=quality_rejections,
                target_topics=missing_topics,
                feedback_by_topic=feedback_by_topic_from_rejections(quality_rejections, missing_topics),
            )
            sections = merge_sections(sections, repair_sections)
    else:
        sections = section_stub(site, publication_date)

    if args.use_llm and candidates and missing_section_topics(site, sections):
        missing_topics = sorted(missing_section_topics(site, sections))
        write_run_artifacts(publication_date, candidates, sections, site, quality_rejections)
        print(
            json.dumps(
                {
                    "ok": False,
                    "date": publication_date,
                    "candidates": len(candidates),
                    "sections": len(sections),
                    "news_items": total_news_count(sections),
                    "missing_sections": missing_topics,
                    "error": "Some sections could not be repaired to at least one quality-gated article.",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    if args.review:
        review_available = True
        try:
            reviewed_sections = review_with_gemini(sections, candidates, site, quality_rejections)
        except GeminiReviewUnavailable as exc:
            review_available = False
            log_gemini_review_unavailable(quality_rejections, sections, exc)
            reviewed_sections = sections
        missing_topics = missing_section_topics(site, reviewed_sections)
        repair_attempt = 0
        while review_available and missing_topics and repair_attempt < POST_REVIEW_REPAIR_ATTEMPTS:
            repair_attempt += 1
            repair_sections = call_deepseek_for_sections(
                candidates,
                site,
                publication_date,
                rejections=quality_rejections,
                target_topics=missing_topics,
                feedback_by_topic=feedback_by_topic_from_rejections(quality_rejections, missing_topics),
            )
            if not repair_sections:
                break
            try:
                reviewed_repair_sections = review_with_gemini(repair_sections, candidates, site, quality_rejections)
            except GeminiReviewUnavailable as exc:
                review_available = False
                log_gemini_review_unavailable(quality_rejections, repair_sections, exc)
                reviewed_repair_sections = repair_sections
            reviewed_sections = merge_sections(reviewed_sections, reviewed_repair_sections)
            missing_topics = missing_section_topics(site, reviewed_sections)
        sections = reviewed_sections
        if review_available and args.use_llm and candidates and missing_section_topics(site, sections):
            missing_topics = sorted(missing_section_topics(site, sections))
            write_run_artifacts(publication_date, candidates, sections, site, quality_rejections)
            print(
                json.dumps(
                    {
                        "ok": False,
                        "date": publication_date,
                        "candidates": len(candidates),
                        "sections": len(sections),
                        "news_items": total_news_count(sections),
                        "missing_sections": missing_topics,
                        "error": "Some sections still lacked one evidence-approved article after repair attempts.",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1

    annotate_cross_section_duplicates(sections, site)

    if args.tts:
        synthesize_tts(sections, publication_date)

    render_site(site, sections, publication_date, preserve_audio=args.tts)
    write_run_artifacts(publication_date, candidates, sections, site, quality_rejections)
    print(
        json.dumps(
            {
                "ok": True,
                "date": publication_date,
                "candidates": len(candidates),
                "sections": len(sections),
                "news_items": total_news_count(sections),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
