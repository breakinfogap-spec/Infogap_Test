"""Idempotent schedule guard and seven-day published-source ledger (no credentials)."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SITE_URL = "https://daily-impact-news.break-infogap.workers.dev"


def remote_json(path):
    request = Request(SITE_URL + path + "?fresh=" + str(dt.datetime.now().timestamp()),
                      headers={"User-Agent": "InfoGap-Publication-Check/1.0", "Cache-Control": "no-cache"})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def source_urls(sections):
    return sorted({c[key] for s in sections for item in s.get("news_items", [])
                   for c in item.get("citations", []) for key in ("url", "discovery_url") if c.get(key)})


def load_history():
    history = {}
    try:
        history.update(remote_json("/data/publication-history.json").get("days", {}))
    except Exception:
        pass  # First deployment has no ledger yet; seed from the current published edition.
    try:
        current = remote_json("/data/index.json")
        history[current["publication_date"]] = source_urls(current["sections"])
    except Exception:
        pass
    return history


def needs_publication(target, current):
    return str(current.get("publication_date", "")) < target


def validate_edition(data):
    from pipeline import analysis_char_count, MIN_NEWS_CHARS, MAX_NEWS_CHARS
    site = json.loads((ROOT / "config/site.json").read_text(encoding="utf-8"))
    sections = data.get("sections", [])
    if {s["topic"] for s in sections} != {t["id"] for t in site["topics"]}:
        raise ValueError("Cannot publish: missing sections")
    for section in sections:
        items = section.get("news_items", [])
        if not 1 <= len(items) <= 3 or not any(i.get("wallet_impact_anchor") for i in items):
            raise ValueError("Cannot publish: section count/wallet gate failed")
        for item in items:
            count = analysis_char_count(item["body_markdown"], item["impact_markdown"])
            if not MIN_NEWS_CHARS <= count <= MAX_NEWS_CHARS or not item.get("evidence_review_passed") or not item.get("citations"):
                raise ValueError("Cannot publish: length/evidence/citation gate failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["check", "ledger", "mark"])
    parser.add_argument("--date", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    today = dt.datetime.now(ZoneInfo("America/Vancouver")).date()
    target = args.date or (today - dt.timedelta(days=1)).isoformat()
    target = dt.date.fromisoformat(target).isoformat()
    if args.mode == "check":
        needed = True
        try:
            needed = needs_publication(target, remote_json("/data/index.json"))
        except Exception:
            pass  # An unavailable check must not permanently suppress recovery.
        needed = needed or args.force
        output = f"date={target}\nneeded={str(needed).lower()}\n"
        print(output, end="")
        if os.getenv("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write(output)
    elif args.mode == "ledger":
        directory = ROOT / "generated/site/data"
        data = json.loads((directory / "index.json").read_text(encoding="utf-8"))
        validate_edition(data)
        history = load_history()
        history[target] = source_urls(data["sections"])
        cutoff = (dt.date.fromisoformat(target) - dt.timedelta(days=7)).isoformat()
        history = {day: urls for day, urls in history.items() if cutoff <= day <= target}
        (directory / "publication-history.json").write_text(json.dumps({"days": history}), encoding="utf-8")
    else:
        # Only invoked after the remote upload succeeds.
        path = ROOT / "runs" / target / "published.json"
        path.write_text(json.dumps({"date": target, "published": True}), encoding="utf-8")


if __name__ == "__main__":
    main()
