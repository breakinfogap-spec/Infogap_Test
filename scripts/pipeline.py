from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import os
import re
import shutil
import sys
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
MIN_NEWS_CHARS = 650
SINGLE_ITEM_SECTION_MIN_CHARS = 900
MAX_HEADINGS_PER_ITEM = 2
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


def call_deepseek_for_sections(candidates: list[dict], site: dict, date_text: str) -> list[dict]:
    api_key = os.environ["DEEPSEEK_API_KEY"]
    model = os.getenv("LLM_PRIMARY_MODEL", "deepseek-chat")
    numbered_candidates = number_candidates(candidates, site)
    prompt = {
        "date": date_text,
        "audience": "ordinary Canadians; Vancouver local readers have a dedicated section",
        "topics": site["topics"],
        "editorial_rules": site["editorial_rules"],
        "source_rules": [
            "Sources are identified only by refs like [S1]. You must not output URLs.",
            "Every specific claim about money, dates, eligibility, deadlines, quotes, or opposing views must cite one or more source refs.",
            "Use only source_refs that appear in the provided candidates.",
            "Use topic_candidates as routing hints. If a candidate includes local_vancouver, prefer the 温哥华本地 section unless another section is clearly more important.",
            "BC/Vancouver airport, school, city service, safety, housing, transit, business, or local politics stories should go to local_vancouver or living, not only technology.",
        ],
        "writing_template": {
            "section_overview": "今天有 X 条新闻会对我们的生活造成影响。",
            "per_news_item": [
                "A clear, forceful title that directly names the core issue or viewpoint.",
                "Full readable Chinese paragraphs, not bullet points.",
                "Follow the NEWS_ANALYST_SYSTEM_PROMPT style and structure.",
                "First summarize what happened in one sentence, then directly explain what it means.",
                "Use mostly full paragraphs. Markdown ## question-style subheadings are optional, not mandatory.",
                "Use 0-2 subheadings per news item only when they introduce genuinely distinct questions; never add a heading before every paragraph.",
                "Include key data, important statements, different viewpoints, opposition, and relevant precedent when source-backed.",
                "End impact_markdown with the fixed format: 这个对于[具体群体]的影响是： [短期] / [中期] / [长期].",
                "Do not use em dashes or Chinese dash punctuation.",
            ],
            "section_composition": "Target 2-3 news items per section when evidence supports them. Avoid weak single-item sections; if a section has only one news item, it must be a deeper source-backed feature, not a short explainer.",
            "local_section_rule": "If any candidates have topic_candidates containing local_vancouver, generate a 温哥华本地 section from the strongest local candidates unless all are irrelevant to residents.",
            "length_target": "Each news item must be 800-1200 Chinese characters when evidence supports it. Do not return short 400-600 character explainers.",
        },
        "candidates": candidates_for_prompt(numbered_candidates),
        "required_json_shape": {
            "sections": [
                {
                    "topic": "finance|technology|living|immigration|local_vancouver",
                    "overview": "今天有 X 条新闻会对我们的生活造成影响。",
                    "news_items": [
                        {
                            "title": "clear Chinese headline that directly names the core issue or viewpoint",
                            "summary": "one sentence",
                            "body_markdown": "article-like analysis in Simplified Chinese following NEWS_ANALYST_SYSTEM_PROMPT, with [S1] refs, no bullet points, and 0-2 optional Markdown ## subheadings",
                            "impact_markdown": "fixed final impact format: 这个对于[具体群体]的影响是： [短期] / [中期] / [长期], with [S1] refs",
                            "source_refs": ["S1", "S2"],
                            "tts_text": "plain spoken Chinese text based only on title/body/impact",
                        }
                    ],
                }
            ]
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
                        + "\n\n硬性技术规则：Return strict JSON only. Never output source URLs. "
                        "Only cite source refs like [S1]. Write complete article-style analysis, not outlines. "
                        "Each news item should usually be at least 800 Chinese characters. "
                        "Do not write bullet points in the article body or impact text."
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
    text = response.json()["choices"][0]["message"]["content"]
    try:
        payload = json.loads(extract_json(text))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DeepSeek returned invalid JSON after {len(text)} characters: {exc}") from exc
    return normalize_section_reports(payload.get("sections", []), numbered_candidates, site, date_text)


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


def normalize_section_reports(raw_sections: list[dict], sources: list[dict], site: dict, date_text: str) -> list[dict]:
    source_by_ref = {source["ref"]: source for source in sources}
    allowed_topics = topic_ids(site)
    reports = []

    for raw_section in raw_sections:
        topic = str(raw_section.get("topic") or "").strip()
        if topic not in allowed_topics:
            continue

        news_items = []
        for raw_item in raw_section.get("news_items") or []:
            title = str(raw_item.get("title") or "").strip()
            body_markdown = limit_headings(str(raw_item.get("body_markdown") or "").strip())
            impact_markdown = str(raw_item.get("impact_markdown") or "").strip()
            if not title or not body_markdown or not impact_markdown:
                continue
            if has_bullet_markers(body_markdown) or has_bullet_markers(impact_markdown):
                continue
            if has_dash_punctuation(body_markdown) or has_dash_punctuation(impact_markdown):
                continue
            item_heading_count = heading_count(body_markdown)
            item_char_count = analysis_char_count(body_markdown, impact_markdown)
            if item_char_count < MIN_NEWS_CHARS:
                continue
            combined_text = "\n".join([title, body_markdown, impact_markdown, str(raw_item.get("tts_text") or "")])
            if has_source_url(combined_text):
                continue

            refs = normalize_source_refs(raw_item.get("source_refs"), combined_text, source_by_ref)
            if not refs:
                continue

            tts_text = str(raw_item.get("tts_text") or "").strip()
            if not tts_text:
                tts_text = "\n\n".join([title, body_markdown, "对我们的影响", impact_markdown])

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
                }
            )

        if not news_items:
            continue
        if len(news_items) == 1 and news_items[0]["analysis_chars"] < SINGLE_ITEM_SECTION_MIN_CHARS:
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


def truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


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
            "avoid weak single-item sections unless the single item is a deeper feature; "
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
        filename = f"{section['topic']}.mp3"
        output = audio_dir / filename
        spoken_text = "\n\n".join(
            [section["overview"]]
            + [
                "\n\n".join([item["title"], item["tts_text"]])
                for item in section["news_items"]
            ]
        )
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=truncate_utf8(spoken_text, 4800)),
            voice=texttospeech.VoiceSelectionParams(language_code="cmn-CN", name=voice_name),
            audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3),
        )
        output.write_bytes(response.audio_content)
        section["audio_path"] = f"audio/{date_text}/{filename}"


def sections_for_render(site: dict, sections: list[dict], publication_date: str) -> list[dict]:
    by_topic = {section["topic"]: section for section in sections}
    rendered = []
    for topic in site.get("topics", []):
        report = by_topic.get(topic["id"])
        if report:
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


def write_run_artifacts(date_text: str, candidates: list[dict], sections: list[dict], site: dict) -> None:
    run_dir = RUNS / date_text
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "sections.json").write_text(json.dumps(sections, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "source-candidates.json").write_text(
        json.dumps(number_candidates(candidates, site), ensure_ascii=False, indent=2),
        encoding="utf-8",
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

    if args.use_llm:
        sections = call_deepseek_for_sections(candidates, site, args.date)
    else:
        sections = section_stub(site, args.date)

    if args.use_llm and candidates and not sections:
        write_run_artifacts(args.date, candidates, sections, site)
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
    write_run_artifacts(args.date, candidates, sections, site)
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
