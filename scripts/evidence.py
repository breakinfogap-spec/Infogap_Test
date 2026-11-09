"""Fetch publicly accessible article evidence; never bypass access controls."""
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
import re

import requests
import trafilatura
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; InfoGapBot/1.1; +https://github.com/breakinfogap-spec/Infogap_Test)"}


def clean_text(value):
    value = str(value or "")
    if "<" not in value:
        return value.strip()
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def fetch_evidence(candidate):
    """Keep discovery URL stable so source references and deduplication stay stable."""
    result = dict(candidate)
    result["summary"] = clean_text(result.get("summary"))
    try:
        response = requests.get(result["url"], headers=HEADERS, timeout=12)
        response.raise_for_status()
        # Google search metadata is not article evidence. Resolve only explicit publisher links.
        if urlparse(response.url).hostname == "news.google.com":
            soup = BeautifulSoup(response.text, "html.parser")
            title_words = set(re.findall(r"[a-z]{4,}", candidate.get("title", "").lower()))
            # Never treat an unrelated navigation/advertising link as the source article.
            links = [a.get("href", "") for a in soup.select('a[href^="https://"]')
                     if len(title_words.intersection(re.findall(r"[a-z]{4,}", a.get_text().lower()))) >= 3]
            links = [u for u in links if not re.search(r"https://[^/]*(?:google|youtube)\.", u)]
            if not links:
                raise ValueError("publisher_link_unavailable")
            response = requests.get(links[0], headers=HEADERS, timeout=12)
            response.raise_for_status()
        text = trafilatura.extract(response.text, include_comments=False, include_tables=True) or ""
        if len(text) < 300:
            raise ValueError("article_body_unavailable")
        result.update(evidence_text=text[:12000], evidence_url=response.url, evidence_status="body")
    except Exception as exc:
        result.update(evidence_status="summary_only", evidence_error=type(exc).__name__)
    result["evidence_attempts"] = int(candidate.get("evidence_attempts", 0)) + 1
    return result


def enrich_candidates(candidates, selected_urls, retry=False):
    pending = [c for c in candidates if c["url"] in selected_urls
               and not c.get("evidence_text")
               and int(c.get("evidence_attempts", 0)) < (2 if retry else 1)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        fetched = {c["url"]: c for c in pool.map(fetch_evidence, pending)}
    for candidate in candidates:
        candidate.update(fetched.get(candidate["url"], {}))
    return candidates
