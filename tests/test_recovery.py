import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evidence
import publication
import pipeline
from test_pipeline import site, valid_item, non_wallet_item, source, FakeResponse


class EvidenceRecoveryTests(unittest.TestCase):
    def test_main_replaces_rejected_evidence_and_retains_approved_sections(self):
        candidates = [source(topic="finance"), source(ref="S2", topic="technology")]
        for candidate in candidates:
            candidate["evidence_text"] = "Publicly verified body"
        finance = {"topic": "finance", "news_items": [{"wallet_impact_anchor": True}]}
        technology = {"topic": "technology", "news_items": [{"wallet_impact_anchor": True}]}
        requests_by_topic = []

        def generate(*args, **kwargs):
            requests_by_topic.append(kwargs["target_topics"])
            return [finance, technology] if len(requests_by_topic) == 1 else [technology]

        def review(sections, pool, settings, rejections):
            if len(requests_by_topic) == 1:
                rejections.append({"topic": "technology", "reason": "gemini_evidence_review_rejected", "source_refs": ["S2"]})
                return [finance]
            self.assertIn("technology", pool[1]["excluded_topics"])
            return sections

        with patch.object(sys, "argv", ["pipeline.py", "--date", "2026-11-08", "--use-llm", "--review"]), \
             patch.object(pipeline, "load_json", side_effect=[dict(site("finance", "technology"), timezone="America/Vancouver"), {}]), \
             patch.object(pipeline, "collect_candidates_for_publication", return_value=candidates), \
             patch.object(pipeline, "enrich_candidates"), \
             patch.object(pipeline, "call_deepseek_for_sections", side_effect=generate), \
             patch.object(pipeline, "review_with_gemini", side_effect=review), \
             patch.object(pipeline, "write_run_artifacts"), \
             patch.object(pipeline, "annotate_cross_section_duplicates"), \
             patch.object(pipeline, "render_site"):
            self.assertEqual(0, pipeline.main())
        self.assertEqual([{"finance", "technology"}, {"technology"}], requests_by_topic)

    def test_body_is_preserved_for_both_writer_and_reviewer(self):
        candidate = source()
        candidate.update(evidence_text="Verified article body " * 30, evidence_url="https://example.com/original")
        numbered = pipeline.number_candidates([candidate], site("technology"))
        self.assertEqual(candidate["evidence_text"], pipeline.candidates_for_prompt(numbered)[0]["article_evidence"])
        self.assertEqual(candidate["evidence_url"], pipeline.citations_for_refs(["S1"], {"S1": numbered[0]})[0]["url"])

    def test_fetch_failure_keeps_candidate_and_cleans_link_markup(self):
        candidate = {"url": "https://example.com/news", "summary": '<a href="/x">News title</a>'}
        with patch.object(evidence.requests, "get", side_effect=evidence.requests.Timeout):
            result = evidence.fetch_evidence(candidate)
        self.assertEqual("summary_only", result["evidence_status"])
        self.assertEqual("News title", result["summary"])
        self.assertNotIn("evidence_text", result)

    def test_body_recovery_does_not_repeat_successful_fetches(self):
        candidate = {"url": "https://example.com/news", "evidence_text": "already fetched"}
        with patch.object(evidence, "fetch_evidence") as fetch:
            evidence.enrich_candidates([candidate], {candidate["url"]}, retry=True)
        fetch.assert_not_called()

    def test_review_cannot_remove_only_wallet_article_and_leave_section_valid(self):
        section = pipeline.normalize_section_reports([
            {"topic": "technology", "news_items": [valid_item("产品价格"), non_wallet_item("公共讨论")]}
        ], [source()], site("technology"), "2026-11-08", [])[0]
        reviews = [{"topic": "technology", "article_id": i["id"], "ok": n != 0}
                   for n, i in enumerate(section["news_items"])]
        response = FakeResponse({"candidates": [{"content": {"parts": [{"text": json.dumps({"reviews": reviews})}]}}]})
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test"}), patch.object(pipeline, "post_gemini_review", return_value=response):
            self.assertEqual([], pipeline.review_with_gemini([section], [source()], site("technology"), []))

    def test_published_ledger_excludes_unpublished_runs_and_includes_old_boundary(self):
        with patch.object(pipeline, "load_history", return_value={"2026-11-01": ["https://example.com/used"]}):
            self.assertIn("https://example.com/used", pipeline.used_source_urls("2026-11-08", 7))


class PublicationGuardTests(unittest.TestCase):
    def test_publication_gate_requires_review_for_every_article(self):
        data = {"sections": [{"topic": t, "news_items": [dict(valid_item("消费价格"), wallet_impact_anchor=True,
                    citations=[{"url": "https://example.com"}], evidence_review_passed=True)]}
                    for t in ("finance", "technology", "living", "immigration", "local_vancouver")]}
        publication.validate_edition(data)
        data["sections"][0]["news_items"][0]["evidence_review_passed"] = False
        with self.assertRaises(ValueError):
            publication.validate_edition(data)

    def test_current_or_newer_editions_skip_expensive_recovery(self):
        for day in ("2026-11-08", "2026-11-09"):
            self.assertFalse(publication.needs_publication("2026-11-08", {"publication_date": day}))
        self.assertTrue(publication.needs_publication("2026-11-08", {"publication_date": "2026-11-07"}))

    def test_ledger_records_publisher_and_discovery_urls(self):
        urls = publication.source_urls([{"news_items": [{"citations": [{"url": "publisher", "discovery_url": "search"}]}]}])
        self.assertEqual(["publisher", "search"], urls)


if __name__ == "__main__":
    unittest.main()
