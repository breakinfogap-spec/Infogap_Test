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
MIN_NEWS_ITEMS_PER_SECTION = 2
MAX_HEADINGS_PER_ITEM = 2
TTS_CHUNK_MAX_BYTES = 4000
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
最后另起一段，固定格式：
这个对于[具体群体]的影响是：
[短期] / [中期] / [长期]

## 风格规则

用高中生的词汇水平。
每段 5-7 句话，不要太长。
不用专业术语，必须出现时加一句解释。
不用破折号。
像跟朋友聊天一样解释，不是写报告。"""


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def vancouver_window(date_text: str, timezone: str) -> tuple[dt.datetime, dt.datetime]:
    zone = ZoneInfo(timezone)
    start = dt.datetime.fromisoformat(date_text).replace(tzinfo=zone)
    return start, start + dt.timedelta(days=1)


def entry_datetime(entry: dict) -> dt.datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)


def collect_candidates(source_registry: dict, date_text: str, timezone: str) -> list[dict]:
    start, end = vancouver_window(date_text, timezone)
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
                headers={"User-Agent": "InfoGapTest/0.1 (+https://github.com/breakinfogap-spec/Infogap_Test)"},
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
                }
            )

    collect_candidates.last_debug = source_debug
    return candidates


collect_candidates.last_debug = []


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
        }
        for item in candidates
    ]


def candidates_for_section(candidates: list[dict], topic: str) -> list[dict]:
    return [candidate for candidate in candidates if topic in candidate.get("topic_candidates", [])]


def log_quality_rejection(rejections: list[dict], topic: str, reason: str, **details: object) -> None:
    entry = {"topic": topic, "reason": reason, **details}
    rejections.append(entry)
    print(json.dumps({"quality_gate_rejection": entry}, ensure_ascii=False), file=sys.stderr)


def call_deepseek_for_sections(
    candidates: list[dict], site: dict, date_text: str, rejections: list[dict] | None = None
) -> list[dict]:
    if rejections is None:
        rejections = []
    api_key = os.environ["DEEPSEEK_API_KEY"]
    model = os.getenv("LLM_PRIMARY_MODEL", "deepseek-chat")
    numbered_candidates = number_candidates(candidates, site)
    reports = []

    for topic in site["topics"]:
        topic_id = topic["id"]
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

        prompt = {
            "date": date_text,
            "audience": "ordinary Canadians; Vancouver local readers have a dedicated section",
            "target_section": topic,
            "editorial_rules": site["editorial_rules"],
            "source_rules": [
                "Sources are identified only by refs like [S1]. You must not output URLs.",
                "Every specific claim about money, dates, eligibility, deadlines, quotes, or opposing views must cite one or more source refs.",
                "Use only source_refs that appear in the provided candidates.",
                "Write only the requested target_section. Do not return other sections.",
            ],
            "writing_template": {
                "section_overview": "今天有 X 条新闻会对我们的生活造成影响。",
                "section_composition": "Return 2-3 distinct, evidence-backed news analyses. A section with fewer than two accepted articles will not be published.",
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
                            "Only cite source refs like [S1]. Return at least two complete articles when evidence supports them. "
                            "Each article must be 800-1500 Chinese characters. Do not write bullet points. "
                            "The fixed impact block belongs once at the very end of each article."
                        ),
                    },
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                "temperature": 0.2,
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
            log_quality_rejection(rejections, topic_id, "model_returned_no_section", candidate_count=len(topic_candidates))
            continue
        matching_sections = []
        for returned_section in raw_sections:
            returned_topic = str(returned_section.get("topic") or "").strip()
            if returned_topic != topic_id:
                log_quality_rejection(
                    rejections,
                    topic_id,
                    "model_returned_wrong_section",
                    returned_topic=returned_topic,
                )
                continue
            matching_sections.append(returned_section)
        if not matching_sections:
            continue
        reports.extend(
            normalize_section_reports(matching_sections, numbered_candidates, site, date_text, rejections=rejections)
        )

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


def has_valid_impact_block(body_markdown: str, impact_markdown: str) -> bool:
    if IMPACT_PREFIX_RE.search(body_markdown):
        return False
    matches = list(IMPACT_PREFIX_RE.finditer(impact_markdown))
    if len(matches) != 1 or impact_markdown[: matches[0].start()].strip():
        return False
    short_pos = impact_markdown.find("[短期]", matches[0].end())
    middle_pos = impact_markdown.find("[中期]", short_pos + 1)
    long_pos = impact_markdown.find("[长期]", middle_pos + 1)
    return short_pos >= 0 and short_pos < middle_pos < long_pos


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
        topic = str(raw_section.get("topic") or "").strip()
        if topic not in allowed_topics:
            log_quality_rejection(rejections, topic or "unknown", "invalid_topic")
            continue

        news_items = []
        raw_items = raw_section.get("news_items") or []
        for item_index, raw_item in enumerate(raw_items, start=1):
            title = str(raw_item.get("title") or "").strip()
            body_markdown = limit_headings(str(raw_item.get("body_markdown") or "").strip())
            impact_markdown = str(raw_item.get("impact_markdown") or "").strip()
            rejection_details = {"item_index": item_index, "title": title[:120]}
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
                "section_has_fewer_than_two_accepted_articles",
                candidate_articles=len(raw_items),
                accepted_articles=len(news_items),
            )
            continue

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


def split_oversized_text(text: str, max_bytes: int) -> list[str]:
    pieces = []
    remaining = text
    while remaining:
        encoded = remaining.encode("utf-8")
        if len(encoded) <= max_bytes:
            pieces.append(remaining)
            break
        cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
        preferred = max(cut.rfind(mark) for mark in ("，", "、", ",", " "))
        if preferred > 0:
            cut = cut[: preferred + 1]
        cut = cut.rstrip()
        if not cut:
            cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
        pieces.append(cut)
        remaining = remaining[len(cut) :].lstrip()
    return pieces


def split_tts_text(text: str, max_bytes: int = TTS_CHUNK_MAX_BYTES) -> list[str]:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    units = []
    for paragraph in paragraphs:
        if len(paragraph.encode("utf-8")) <= max_bytes:
            units.append(paragraph)
            continue
        sentences = [part.strip() for part in re.split(r"(?<=[。！？!?；;])", paragraph) if part.strip()]
        for sentence in sentences:
            if len(sentence.encode("utf-8")) <= max_bytes:
                units.append(sentence)
            else:
                units.extend(split_oversized_text(sentence, max_bytes))

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


def review_with_gemini(sections: list[dict]) -> None:
    if not sections:
        return
    api_key = os.environ["GEMINI_API_KEY"]
    model = os.getenv("LLM_REVIEW_MODEL", "gemini-3.5-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    prompt = {
        "task": (
            "Check these Chinese section reports. Return JSON with ok true/false. "
            "They must read like clear news explainers for ordinary readers, not bullet points or formal reports; "
            "titles should directly name the core issue or viewpoint; include multiple viewpoints where source-backed; "
            "use full paragraphs with at most two useful subheadings per news item; avoid heading-heavy outlines; "
            "avoid em dashes; end with a final impact paragraph using the fixed short/mid/long term format; "
            "reject every section with fewer than two accepted articles; each article must be 800-1500 Chinese characters; "
            "and every specific money/date/eligibility/deadline claim must be supported by source refs."
        ),
        "sections": [
            {
                "topic": section["topic"],
                "overview": section["overview"],
                "news_items": [
                    {
                        "title": item["title"],
                        "body_markdown": item["body_markdown"][:4000],
                        "impact_markdown": item["impact_markdown"][:1000],
                        "source_refs": item["source_refs"],
                        "analysis_chars": item.get("analysis_chars"),
                        "heading_count": item.get("heading_count"),
                    }
                    for item in section["news_items"]
                ],
            }
            for section in sections
        ],
    }
    response = requests.post(
        url,
        params={"key": api_key},
        json={"contents": [{"parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}]},
        timeout=60,
    )
    response.raise_for_status()
    text = "".join(part.get("text", "") for part in response.json()["candidates"][0]["content"]["parts"])
    if "false" in text.lower() and "ok" in text.lower():
        raise RuntimeError(f"Gemini review flagged the draft: {text[:500]}")


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

    from google.cloud import texttospeech

    client = texttospeech.TextToSpeechClient()
    voice_name = os.getenv("TTS_VOICE", "cmn-CN-Wavenet-A")
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
            for text_chunk in text_chunks:
                response = client.synthesize_speech(
                    input=texttospeech.SynthesisInput(text=text_chunk),
                    voice=texttospeech.VoiceSelectionParams(language_code="cmn-CN", name=voice_name),
                    audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3),
                )
                audio_segments.append(response.audio_content)
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
    return [
        by_topic[topic["id"]]
        for topic in site.get("topics", [])
        if topic["id"] in by_topic and len(by_topic[topic["id"]].get("news_items", [])) >= MIN_NEWS_ITEMS_PER_SECTION
    ]


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
    args = parser.parse_args()

    site = load_json(ROOT / "config" / "site.json")
    source_registry = load_json(ROOT / "config" / "source-registry.json")
    candidates = collect_candidates(source_registry, args.date, site["timezone"])

    if not candidates and not args.allow_empty:
        print("No enabled source produced candidates. Enable verified feeds before the real run.", file=sys.stderr)
        print(json.dumps({"source_debug": collect_candidates.last_debug}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    quality_rejections = []
    if args.use_llm:
        sections = call_deepseek_for_sections(candidates, site, args.date, rejections=quality_rejections)
    else:
        sections = section_stub(site, args.date)

    if args.use_llm and candidates and not sections:
        write_run_artifacts(args.date, candidates, sections, site, quality_rejections)
        print(
            json.dumps(
                {
                    "ok": False,
                    "date": args.date,
                    "candidates": len(candidates),
                    "sections": 0,
                    "news_items": 0,
                    "error": "LLM output did not pass section report quality gates.",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    if args.review:
        review_with_gemini(sections)

    if args.tts:
        synthesize_tts(sections, args.date)

    render_site(site, sections, args.date, preserve_audio=args.tts)
    write_run_artifacts(args.date, candidates, sections, site, quality_rejections)
    print(
        json.dumps(
            {
                "ok": True,
                "date": args.date,
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
