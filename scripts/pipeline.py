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


def article_stub(site: dict, publication_date: str) -> list[dict]:
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


def call_deepseek_for_articles(candidates: list[dict], site: dict, date_text: str) -> list[dict]:
    api_key = os.environ["DEEPSEEK_API_KEY"]
    model = os.getenv("LLM_PRIMARY_MODEL", "deepseek-chat")
    prompt = {
        "date": date_text,
        "audience": "ordinary Canadians, with Vancouver-specific analysis when applicable",
        "topics": site["topics"],
        "editorial_rules": site["editorial_rules"],
        "candidates": candidates[:80],
        "required_json_shape": {
            "articles": [
                {
                    "topic": "finance|technology|living|immigration",
                    "title": "Chinese title",
                    "summary": "one sentence",
                    "body_markdown": "analysis in Simplified Chinese",
                    "citations": [{"title": "source title", "url": "https://..."}],
                    "tts_text": "plain spoken Chinese text",
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
                {"role": "system", "content": "You are an evidence-first Chinese news analyst. Return strict JSON only."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "temperature": 0.2,
        },
        timeout=90,
    )
    response.raise_for_status()
    text = response.json()["choices"][0]["message"]["content"]
    payload = json.loads(extract_json(text))
    return normalize_articles(payload.get("articles", []), date_text)


def extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return text


def normalize_articles(raw_articles: list[dict], date_text: str) -> list[dict]:
    articles = []
    allowed_topics = {"finance", "technology", "living", "immigration"}
    for item in raw_articles:
        title = str(item.get("title", "")).strip()
        topic = str(item.get("topic", "")).strip()
        citations = item.get("citations") or []
        if not title or topic not in allowed_topics or not citations:
            continue
        article = {
            "topic": topic,
            "title": title,
            "slug": make_slug(title, date_text),
            "summary": str(item.get("summary", "")).strip(),
            "body_markdown": str(item.get("body_markdown", "")).strip(),
            "body_html": simple_markdown_to_html(str(item.get("body_markdown", "")).strip()),
            "tts_text": str(item.get("tts_text") or item.get("body_markdown") or title).strip(),
            "citations": [
                {"title": str(c.get("title", "")).strip(), "url": str(c.get("url", "")).strip()}
                for c in citations
                if str(c.get("url", "")).startswith("http")
            ],
            "publication_date": date_text,
            "audio_path": "",
        }
        if article["citations"]:
            articles.append(article)
    return articles


def review_with_gemini(articles: list[dict]) -> None:
    if not articles:
        return
    api_key = os.environ["GEMINI_API_KEY"]
    model = os.getenv("LLM_REVIEW_MODEL", "gemini-3.5-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    prompt = {
        "task": "Check whether each article has citations and avoids unsupported claims about dates, money, eligibility, and jurisdiction. Return JSON with ok true/false.",
        "articles": [
            {
                "title": a["title"],
                "topic": a["topic"],
                "body_markdown": a["body_markdown"][:3000],
                "citations": a["citations"],
            }
            for a in articles
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


def synthesize_tts(articles: list[dict], date_text: str) -> None:
    if not articles:
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

    for article in articles:
        filename = f"{article['slug'].split('/')[-1]}.mp3"
        output = audio_dir / filename
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=article["tts_text"][:4500]),
            voice=texttospeech.VoiceSelectionParams(language_code="cmn-CN", name=voice_name),
            audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3),
        )
        output.write_bytes(response.audio_content)
        article["audio_path"] = f"audio/{date_text}/{filename}"


def topic_name(topic_id: str, topics: list[dict]) -> str:
    for topic in topics:
        if topic["id"] == topic_id:
            return topic["name"]
    return topic_id


def render_site(site: dict, articles: list[dict], publication_date: str) -> None:
    if GENERATED.exists():
        shutil.rmtree(GENERATED)
    GENERATED.mkdir(parents=True)

    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.globals["topic_name"] = topic_name

    shutil.copyfile(ROOT / "templates" / "base.css", GENERATED / "base.css")

    (GENERATED / "index.html").write_text(
        env.get_template("index.html.j2").render(site=site, articles=articles, publication_date=publication_date),
        encoding="utf-8",
    )

    topics_dir = GENERATED / "topics"
    topics_dir.mkdir()
    for topic in site["topics"]:
        topic_articles = [article for article in articles if article["topic"] == topic["id"]]
        (topics_dir / f"{topic['id']}.html").write_text(
            env.get_template("topic.html.j2").render(site=site, topic=topic, articles=topic_articles),
            encoding="utf-8",
        )

    for article in articles:
        article_path = GENERATED / "articles" / f"{article['slug']}.html"
        article_path.parent.mkdir(parents=True, exist_ok=True)
        article_path.write_text(
            env.get_template("article.html.j2").render(site=site, article=article),
            encoding="utf-8",
        )

    data_dir = GENERATED / "data"
    data_dir.mkdir()
    (data_dir / "index.json").write_text(
        json.dumps({"publication_date": publication_date, "articles": articles}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_run_artifacts(date_text: str, candidates: list[dict], articles: list[dict]) -> None:
    run_dir = RUNS / date_text
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "articles.json").write_text(json.dumps(articles, ensure_ascii=False, indent=2), encoding="utf-8")


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
        articles = call_deepseek_for_articles(candidates, site, args.date)
    else:
        articles = article_stub(site, args.date)

    if args.review:
        review_with_gemini(articles)

    if args.tts:
        synthesize_tts(articles, args.date)

    render_site(site, articles, args.date)
    write_run_artifacts(args.date, candidates, articles)
    print(json.dumps({"ok": True, "date": args.date, "candidates": len(candidates), "articles": len(articles)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
