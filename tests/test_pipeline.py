import os
import re
import requests
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline


class FakeResponse:
    def __init__(self, payload, status_code=200, text="", headers=None):
        self.payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)
        return None

    def json(self):
        return self.payload


class FakeFeedResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


def source(ref="S1", topic="technology"):
    return {
        "ref": ref,
        "source_id": "source",
        "source_name": "Source",
        "topic_candidates": [topic],
        "geography": "canada_national",
        "title": "Source title",
        "url": f"https://example.com/{topic}/{ref}",
        "published_at": "2026-09-16T10:00:00-07:00",
        "summary": "Source summary",
    }


def site(*topics):
    return {
        "topics": [{"id": topic, "name": topic, "description": "description"} for topic in topics],
        "editorial_rules": [],
    }


def valid_item(title):
    body = "这条新闻说明普通人的生活选择正在发生变化。[S1]" + "具体影响需要结合事实来理解。" * 52
    body += "\n\n最后，总的来说，这项变化值得持续关注，也需要根据公开信息作出理性判断。[S1]"
    impact = (
        "这个对于加拿大普通家庭的影响是：\n"
        "[短期]需要核对眼前的变化。[S1]\n"
        "[中期]需要调整家庭安排。\n"
        "[长期]需要关注政策与市场走向。"
    )
    return {
        "title": title,
        "summary": "一句话摘要。",
        "body_markdown": body,
        "impact_markdown": impact,
        "source_refs": ["S1"],
    }


class DateInputTests(unittest.TestCase):
    def test_accepts_unpadded_workflow_date(self):
        self.assertEqual("2026-09-17", pipeline.normalize_date_text("2026-9-17"))
        self.assertEqual("2026-09-17", pipeline.normalize_date_text(" 2026-09-17 "))
        self.assertEqual("2026-09-17", pipeline.date_text_days_ago("2026-9-18", 1))

    def test_rejects_invalid_workflow_date(self):
        with self.assertRaisesRegex(ValueError, "Use YYYY-MM-DD"):
            pipeline.normalize_date_text("09/17/2026")


class SourceCollectionTests(unittest.TestCase):
    def test_uses_browser_compatible_infogap_user_agent_and_records_debug_counts(self):
        rss = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><title>Official News</title>
        <item><title>Test story</title><link>https://example.com/story</link>
        <pubDate>Thu, 17 Sep 2026 18:00:00 GMT</pubDate><description>Summary</description></item>
        </channel></rss>"""
        registry = {
            "sources": [
                {
                    "id": "bc_news",
                    "name": "BC Gov News",
                    "topics": ["living"],
                    "geography": "bc_province",
                    "feed_url": "https://news.gov.bc.ca/feed",
                    "enabled": True,
                }
            ]
        }
        with patch.object(pipeline.requests, "get", return_value=FakeFeedResponse(rss)) as request:
            candidates = pipeline.collect_candidates(registry, "2026-09-17", "America/Vancouver")
        self.assertEqual(1, len(candidates))
        self.assertEqual(
            "Mozilla/5.0 (compatible; InfoGapBot/1.0; +https://github.com/breakinfogap-spec/Infogap_Test)",
            request.call_args.kwargs["headers"]["User-Agent"],
        )
        self.assertEqual(1, pipeline.collect_candidates.last_debug[0]["entries"])
        self.assertEqual(1, pipeline.collect_candidates.last_debug[0]["date_matches"])
        self.assertEqual("", pipeline.collect_candidates.last_debug[0]["error"])

    def test_uses_original_publisher_name_from_google_news_entry(self):
        rss = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><title>Google News</title>
        <item><title>Vancouver council approves housing plan</title>
        <link>https://news.google.com/rss/articles/example</link>
        <pubDate>Thu, 17 Sep 2026 18:00:00 GMT</pubDate>
        <description>Housing and city policy</description>
        <source url="https://example.com">CityNews Vancouver</source></item>
        </channel></rss>"""
        registry = {
            "sources": [
                {
                    "id": "google_news_vancouver",
                    "name": "Google News — Vancouver search",
                    "topics": ["local_vancouver"],
                    "geography": "metro_vancouver",
                    "feed_url": "https://news.google.com/rss/search?q=Vancouver%20news",
                    "publisher_from_entry": True,
                    "enabled": True,
                }
            ]
        }
        with patch.object(pipeline.requests, "get", return_value=FakeFeedResponse(rss)):
            candidates = pipeline.collect_candidates(registry, "2026-09-17", "America/Vancouver")
        self.assertEqual("CityNews Vancouver", candidates[0]["source_name"])


class LocalVancouverCandidateTests(unittest.TestCase):
    def test_local_section_only_uses_civic_google_news_search_results(self):
        civic = source(topic="local_vancouver")
        civic.update(
            source_id="google_news_vancouver",
            title="Vancouver city council approves new housing policy",
            geography="metro_vancouver",
        )
        sports = source(ref="S2", topic="local_vancouver")
        sports.update(
            source_id="google_news_vancouver",
            title="Vancouver Canucks announce training camp roster",
            geography="metro_vancouver",
        )
        business_noise = source(ref="S4", topic="local_vancouver")
        business_noise.update(
            source_id="google_news_vancouver",
            source_name="Business in Vancouver",
            title="Vancouver company announces new product",
            geography="metro_vancouver",
        )
        direct_feed = source(ref="S3", topic="local_vancouver")
        direct_feed.update(
            source_id="cbc_bc",
            title="Vancouver transit budget debate",
            geography="bc_province",
        )
        selected = pipeline.candidates_for_section([sports, business_noise, direct_feed, civic], "local_vancouver")
        self.assertEqual(["Vancouver city council approves new housing policy"], [item["title"] for item in selected])

    def test_bc_local_candidates_do_not_leak_into_federal_living_section(self):
        local = source(topic="living")
        local.update(source_id="cbc_bc", geography="bc_province", title="Vancouver housing policy")
        federal = source(ref="S2", topic="living")
        federal.update(source_id="cbc_top", geography="canada_national", title="Federal housing policy")
        selected = pipeline.candidates_for_section([local, federal], "living")
        self.assertEqual(["Federal housing policy"], [item["title"] for item in selected])

    def test_numbering_keeps_sources_that_appear_after_many_google_results(self):
        google_results = []
        for index in range(90):
            item = source(ref=f"G{index}", topic="local_vancouver")
            item.update(
                source_id="google_news_vancouver",
                url=f"https://news.google.com/rss/articles/{index}",
                geography="metro_vancouver",
                title=f"Vancouver housing policy update {index}",
            )
            google_results.append(item)
        finance = source(ref="F1", topic="finance")
        finance["url"] = "https://example.com/finance/after-search-results"
        numbered = pipeline.number_candidates(google_results + [finance], site("finance", "local_vancouver"))
        self.assertIn(finance["url"], [item["url"] for item in numbered])


class QualityGateTests(unittest.TestCase):
    def test_accepts_two_articles_between_800_and_1500_chars(self):
        rejections = []
        reports = pipeline.normalize_section_reports(
            [{"topic": "technology", "news_items": [valid_item("文章一"), valid_item("文章二")]}],
            [source()],
            site("technology"),
            "2026-09-16",
            rejections,
        )
        self.assertEqual(1, len(reports))
        self.assertEqual(2, len(reports[0]["news_items"]))
        for item in reports[0]["news_items"]:
            self.assertGreaterEqual(item["analysis_chars"], 800)
            self.assertLessEqual(item["analysis_chars"], 1500)
        self.assertEqual([], rejections)

    def test_logs_actual_length_and_keeps_one_valid_article(self):
        short = valid_item("短稿")
        short["body_markdown"] = "内容太短。[S1]\n\n最后，总的来说，这是一篇短稿。[S1]"
        rejections = []
        reports = pipeline.normalize_section_reports(
            [{"topic": "technology", "news_items": [short, valid_item("合格稿")]}],
            [source()],
            site("technology"),
            "2026-09-16",
            rejections,
        )
        self.assertEqual(1, len(reports))
        self.assertEqual(1, len(reports[0]["news_items"]))
        length_rejection = next(item for item in rejections if item["reason"] == "article_length_out_of_range")
        self.assertIn("actual_chars", length_rejection)

    def test_moves_trailing_impact_block_out_of_body(self):
        moved = valid_item("自动纠正影响块")
        moved["body_markdown"] += "\n\n" + moved["impact_markdown"]
        moved["impact_markdown"] = ""
        reports = pipeline.normalize_section_reports(
            [{"topic": "technology", "news_items": [moved, valid_item("另一篇")]}],
            [source()],
            site("technology"),
            "2026-09-16",
            [],
        )
        self.assertEqual(1, len(reports))
        corrected = reports[0]["news_items"][0]
        self.assertNotIn("这个对于", corrected["body_markdown"])
        self.assertTrue(corrected["impact_markdown"].startswith("这个对于"))

    def test_rejects_impact_block_in_middle_of_body(self):
        bad = valid_item("影响块位置错误")
        conclusion = "最后，总的来说，这项变化值得持续关注，也需要根据公开信息作出理性判断。[S1]"
        bad["body_markdown"] = bad["body_markdown"].removesuffix(conclusion).rstrip()
        bad["body_markdown"] += (
            "\n\n这个对于读者的影响是：[短期]错误 [中期]错误 [长期]错误"
            "\n\n" + conclusion
        )
        rejections = []
        pipeline.normalize_section_reports(
            [{"topic": "technology", "news_items": [bad, valid_item("另一篇")]}],
            [source()],
            site("technology"),
            "2026-09-16",
            rejections,
        )
        self.assertTrue(any(item["reason"] == "invalid_or_misplaced_impact_block" for item in rejections))

    def test_limits_section_to_three_articles(self):
        reports = pipeline.normalize_section_reports(
            [
                {
                    "topic": "technology",
                    "news_items": [valid_item(f"文章{index}") for index in range(1, 5)],
                }
            ],
            [source()],
            site("technology"),
            "2026-09-16",
            [],
        )
        self.assertEqual(1, len(reports))
        self.assertEqual(3, len(reports[0]["news_items"]))


class CandidateFallbackTests(unittest.TestCase):
    def test_weekly_fallback_skips_previously_used_old_urls(self):
        with tempfile.TemporaryDirectory() as temp_name:
            used_run_dir = Path(temp_name) / "2026-09-15"
            used_run_dir.mkdir(parents=True)
            used_url = "https://example.com/used"
            (used_run_dir / "sections.json").write_text(
                __import__("json").dumps(
                    [
                        {
                            "news_items": [
                                {"citations": [{"url": used_url}]}
                            ]
                        }
                    ]
                ),
                encoding="utf-8",
            )
            window_candidates = [
                {
                    "url": "https://example.com/today",
                    "topic_candidates": ["finance"],
                    "title": "today",
                    "fallback_age_days": 0,
                },
                {
                    "url": used_url,
                    "topic_candidates": ["finance"],
                    "title": "used",
                    "fallback_age_days": 1,
                },
                {
                    "url": "https://example.com/unused",
                    "topic_candidates": ["finance"],
                    "title": "unused",
                    "fallback_age_days": 1,
                },
            ]

            def fake_collect(_registry, date_text, _timezone, lookback_days=1):
                self.assertEqual("2026-09-16", date_text)
                self.assertEqual(2, lookback_days)
                return [dict(item) for item in window_candidates]

            with patch.object(pipeline, "RUNS", Path(temp_name)):
                with patch.object(pipeline, "collect_candidates", side_effect=fake_collect):
                    candidates = pipeline.collect_candidates_for_publication({}, "2026-09-16", "America/Vancouver", 2)
            self.assertEqual(
                ["https://example.com/today", "https://example.com/unused"],
                [candidate["url"] for candidate in candidates],
            )
            self.assertEqual(1, candidates[1]["fallback_age_days"])


class CrossSectionDuplicateTests(unittest.TestCase):
    def test_adds_notice_and_links_for_shared_citation_across_sections(self):
        shared_url = "https://example.com/rate-news"
        sections = [
            {
                "topic": "finance",
                "topic_name": "金融",
                "news_items": [
                    {
                        "id": "rate-finance",
                        "title": "利率影响房贷",
                        "tts_text": "金融正文",
                        "citations": [{"url": shared_url}],
                    }
                ],
            },
            {
                "topic": "living",
                "topic_name": "民生与政策",
                "news_items": [
                    {
                        "id": "rate-living",
                        "title": "利率影响生活成本",
                        "tts_text": "民生正文",
                        "citations": [{"url": shared_url}],
                    }
                ],
            },
        ]
        pipeline.annotate_cross_section_duplicates(sections, site("finance", "living"))
        finance_item = sections[0]["news_items"][0]
        living_item = sections[1]["news_items"][0]

        self.assertIn("民生与政策板块也有分析", finance_item["cross_section_notice"])
        self.assertEqual("/topics/living#rate-living", finance_item["cross_section_links"][0]["href"])
        self.assertTrue(finance_item["tts_text"].startswith(finance_item["cross_section_notice"]))
        self.assertIn("金融板块也有分析", living_item["cross_section_notice"])

    def test_does_not_add_notice_for_unique_citation(self):
        sections = [
            {
                "topic": "finance",
                "topic_name": "金融",
                "news_items": [
                    {
                        "id": "finance-only",
                        "title": "只在金融出现",
                        "tts_text": "正文",
                        "citations": [{"url": "https://example.com/finance-only"}],
                    }
                ],
            }
        ]
        pipeline.annotate_cross_section_duplicates(sections, site("finance"))
        self.assertNotIn("cross_section_notice", sections[0]["news_items"][0])


class DeepSeekTests(unittest.TestCase):
    def candidate(self, topic, ref):
        item = source(ref=ref, topic=topic)
        item.pop("ref")
        return item

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"})
    def test_calls_deepseek_once_per_section(self):
        calls = []

        def fake_post(url, headers, json, timeout):
            prompt = __import__("json").loads(json["messages"][1]["content"])
            topic = prompt["target_section"]["id"]
            calls.append(topic)
            content = __import__("json").dumps({"section": {"topic": topic, "news_items": [valid_item(f"{topic} article")]}})
            return FakeResponse({"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

        with patch.object(pipeline.requests, "post", side_effect=fake_post):
            pipeline.call_deepseek_for_sections(
                [
                    self.candidate("finance", "S1"),
                    self.candidate("finance", "S2"),
                    self.candidate("technology", "S3"),
                    self.candidate("technology", "S4"),
                ],
                site("finance", "technology"),
                "2026-09-16",
                [],
            )
        self.assertEqual(["finance", "technology"], calls)

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"})
    def test_finish_reason_length_fails_before_json_parsing(self):
        response = FakeResponse(
            {"choices": [{"finish_reason": "length", "message": {"content": "{not complete"}}]}
        )
        with patch.object(pipeline.requests, "post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "finish_reason=length"):
                pipeline.call_deepseek_for_sections(
                    [self.candidate("finance", "S1"), self.candidate("finance", "S2")],
                    site("finance"),
                    "2026-09-16",
                    [],
                )

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"})
    def test_accepts_topic_name_and_normalizes_it_to_id(self):
        immigration_site = {
            "topics": [{"id": "immigration", "name": "移民", "description": "description"}],
            "editorial_rules": [],
        }
        content = __import__("json").dumps(
            {"section": {"topic": "移民", "news_items": [valid_item("移民一"), valid_item("移民二")]}}
        )
        response = FakeResponse(
            {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )
        rejections = []
        with patch.object(pipeline.requests, "post", return_value=response):
            reports = pipeline.call_deepseek_for_sections(
                [self.candidate("immigration", "S1"), self.candidate("immigration", "S2")],
                immigration_site,
                "2026-09-16",
                rejections,
            )
        self.assertEqual("immigration", reports[0]["topic"])
        self.assertFalse(any(item["reason"] == "model_returned_wrong_section" for item in rejections))


class GeminiReviewTests(unittest.TestCase):
    def make_section(self, count=3):
        return pipeline.normalize_section_reports(
            [
                {
                    "topic": "technology",
                    "news_items": [valid_item(f"事实核查文章{index}") for index in range(1, count + 1)],
                }
            ],
            [source()],
            site("technology"),
            "2026-09-16",
            [],
        )[0]

    def candidate(self):
        item = source()
        item.pop("ref")
        return item

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test"})
    def test_rejects_one_article_without_stopping_other_articles(self):
        section = self.make_section(3)
        article_ids = [item["id"] for item in section["news_items"]]
        captured_task = []

        def fake_post(url, params, json, timeout):
            request_prompt = __import__("json").loads(json["contents"][0]["parts"][0]["text"])
            captured_task.append(request_prompt["task"])
            reviews = [
                {
                    "topic": "technology",
                    "article_id": article_id,
                    "ok": index != 0,
                    "reasons": ["来源摘要没有支持文中的具体金额"] if index == 0 else [],
                }
                for index, article_id in enumerate(article_ids)
            ]
            content = __import__("json").dumps({"reviews": reviews})
            return FakeResponse(
                {
                    "candidates": [
                        {"finishReason": "STOP", "content": {"parts": [{"text": content}]}}
                    ]
                }
            )

        rejections = []
        with patch.object(pipeline.requests, "post", side_effect=fake_post):
            accepted = pipeline.review_with_gemini(
                [section], [self.candidate()], site("technology"), rejections
            )
        self.assertEqual(2, len(accepted[0]["news_items"]))
        self.assertTrue(any(item["reason"] == "gemini_evidence_review_rejected" for item in rejections))
        task = captured_task[0].lower()
        for forbidden in ("character", "bullet", "dash", "url"):
            self.assertNotIn(forbidden, task)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test"})
    def test_keeps_section_with_one_article_after_gemini(self):
        section = self.make_section(2)
        article_ids = [item["id"] for item in section["news_items"]]
        reviews = [
            {
                "topic": "technology",
                "article_id": article_id,
                "ok": index == 0,
                "reasons": [] if index == 0 else ["结论超出了来源支持范围"],
            }
            for index, article_id in enumerate(article_ids)
        ]
        content = __import__("json").dumps({"reviews": reviews})
        response = FakeResponse(
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": content}]}}]}
        )
        rejections = []
        with patch.object(pipeline.requests, "post", return_value=response):
            accepted = pipeline.review_with_gemini(
                [section], [self.candidate()], site("technology"), rejections
            )
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(accepted[0]["news_items"]))
        self.assertTrue(any(item["reason"] == "gemini_evidence_review_rejected" for item in rejections))

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test"})
    def test_retries_gemini_429_then_succeeds(self):
        section = self.make_section(1)
        article_id = section["news_items"][0]["id"]
        content = __import__("json").dumps(
            {"reviews": [{"topic": "technology", "article_id": article_id, "ok": True, "reasons": []}]}
        )
        success = FakeResponse(
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": content}]}}]}
        )
        calls = [FakeResponse({}, status_code=429, text="quota exceeded"), success]

        def fake_post(*_args, **_kwargs):
            return calls.pop(0)

        with patch.object(pipeline.requests, "post", side_effect=fake_post):
            with patch.object(pipeline.time, "sleep") as sleep:
                accepted = pipeline.review_with_gemini(
                    [section], [self.candidate()], site("technology"), []
                )
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(accepted[0]["news_items"]))
        sleep.assert_called_once_with(10.0)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test"})
    def test_raises_review_unavailable_after_gemini_429_retries_are_exhausted(self):
        section = self.make_section(1)

        def fake_post(*_args, **_kwargs):
            return FakeResponse({}, status_code=429, text="quota exceeded")

        with patch.object(pipeline, "GEMINI_REVIEW_MAX_ATTEMPTS", 2):
            with patch.object(pipeline.requests, "post", side_effect=fake_post):
                with patch.object(pipeline.time, "sleep"):
                    with self.assertRaises(pipeline.GeminiReviewUnavailable):
                        pipeline.review_with_gemini(
                            [section], [self.candidate()], site("technology"), []
                        )

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test"})
    def test_gemini_402_payment_required_is_treated_as_review_unavailable(self):
        section = self.make_section(1)
        response = FakeResponse(
            {},
            status_code=402,
            text='{"error":{"message":"Your prepayment credits are depleted","status":"RESOURCE_EXHAUSTED"}}',
        )
        with patch.object(pipeline.requests, "post", return_value=response):
            with patch.object(pipeline.time, "sleep") as sleep:
                with self.assertRaises(pipeline.GeminiReviewUnavailable):
                    pipeline.review_with_gemini(
                        [section], [self.candidate()], site("technology"), []
                    )
        sleep.assert_not_called()


class TtsTests(unittest.TestCase):
    def test_splits_complete_text_into_chunks_under_4000_bytes(self):
        text = ("第一段内容。" * 350) + "\n\n" + ("第二段内容。" * 350)
        chunks = pipeline.split_tts_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 4000 for chunk in chunks))
        self.assertEqual(text.replace("\n", ""), "".join(chunks).replace("\n", ""))

    def test_splits_long_sentence_even_when_chunk_is_under_4000_bytes(self):
        text = "加拿大普通家庭会感觉压力变化" * 80
        chunks = pipeline.split_tts_text(text, max_bytes=4000, max_sentence_bytes=300)
        pieces = [
            piece.strip()
            for chunk in chunks
            for piece in re.split(r"\n\s*\n", chunk)
            if piece.strip()
        ]
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece.encode("utf-8")) <= 320 for piece in pieces))
        self.assertTrue(all(pipeline.has_sentence_ending(piece) for piece in pieces))

    def test_mp3_byte_concat_reports_duration(self):
        frame = bytes.fromhex("fffb9000") + bytes(413)
        segment = frame * 5
        combined, method = pipeline.concatenate_mp3_segments([segment, segment])
        self.assertEqual(segment + segment, combined)
        self.assertEqual("byte_concat", method)
        self.assertGreater(pipeline.mp3_duration_seconds(combined), 0)

    def test_all_topic_entries_are_rendered_with_empty_placeholders(self):
        sections = [
            {"topic": "finance", "news_items": [{"title": "only one"}]},
            {"topic": "technology", "news_items": [{"title": "one"}, {"title": "two"}]},
        ]
        rendered = pipeline.sections_for_render(site("finance", "technology", "living"), sections, "2026-09-16")
        self.assertEqual(["finance", "technology", "living"], [section["topic"] for section in rendered])
        self.assertEqual(1, len(rendered[0]["news_items"]))
        self.assertEqual(2, len(rendered[1]["news_items"]))
        self.assertEqual([], rendered[2]["news_items"])

    def test_synthesize_tts_creates_one_complete_file_per_article(self):
        frame = bytes.fromhex("fffb9000") + bytes(413)

        class FakeClient:
            def __init__(self):
                self.inputs = []

            def synthesize_speech(self, input, voice, audio_config):
                self.inputs.append(input.text)
                return type("Response", (), {"audio_content": frame * 5})()

        fake_client = FakeClient()
        sections = [
            {
                "topic": "technology",
                "news_items": [
                    {"id": "first", "tts_text": "第一篇。" * 1100},
                    {"id": "second", "tts_text": "第二篇。" * 20},
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            with patch.object(pipeline, "GENERATED", Path(temp_name)):
                with patch("google.cloud.texttospeech.TextToSpeechClient", return_value=fake_client):
                    pipeline.synthesize_tts(sections, "2026-09-16")
            first_path = Path(temp_name) / sections[0]["news_items"][0]["audio_path"]
            second_path = Path(temp_name) / sections[0]["news_items"][1]["audio_path"]
            self.assertTrue(first_path.exists())
            self.assertTrue(second_path.exists())
            self.assertNotEqual(first_path, second_path)
            self.assertGreater(sections[0]["news_items"][0]["audio_segments"], 1)
            self.assertEqual(1, sections[0]["news_items"][1]["audio_segments"])

    def test_synthesize_tts_retries_transient_google_error(self):
        from google.api_core import exceptions as google_exceptions

        frame = bytes.fromhex("fffb9000") + bytes(413)

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def synthesize_speech(self, input, voice, audio_config):
                self.calls += 1
                if self.calls == 1:
                    raise google_exceptions.ServiceUnavailable("temporary outage")
                return type("Response", (), {"audio_content": frame * 5})()

        fake_client = FakeClient()
        sections = [{"topic": "technology", "news_items": [{"id": "retry", "tts_text": "重试测试。" * 20}]}]
        with tempfile.TemporaryDirectory() as temp_name:
            with patch.object(pipeline, "GENERATED", Path(temp_name)):
                with patch.object(pipeline.time, "sleep") as sleep:
                    with patch("google.cloud.texttospeech.TextToSpeechClient", return_value=fake_client):
                        pipeline.synthesize_tts(sections, "2026-09-16")
            output_path = Path(temp_name) / sections[0]["news_items"][0]["audio_path"]
            self.assertTrue(output_path.exists())
        self.assertEqual(2, fake_client.calls)
        sleep.assert_called_once_with(5.0)

    @patch.dict(
        os.environ,
        {"TTS_VOICE": "cmn-CN-Chirp3-HD-Achernar", "TTS_FALLBACK_VOICE": "cmn-CN-Wavenet-A"},
    )
    def test_synthesize_tts_falls_back_to_original_voice(self):
        from google.api_core import exceptions as google_exceptions

        frame = bytes.fromhex("fffb9000") + bytes(413)

        class FakeClient:
            def __init__(self):
                self.voices = []

            def synthesize_speech(self, input, voice, audio_config):
                self.voices.append(voice.name)
                if voice.name == "cmn-CN-Chirp3-HD-Achernar":
                    raise google_exceptions.InvalidArgument("voice unavailable")
                return type("Response", (), {"audio_content": frame * 5})()

        fake_client = FakeClient()
        sections = [{"topic": "technology", "news_items": [{"id": "fallback", "tts_text": "回退测试。" * 20}]}]
        with tempfile.TemporaryDirectory() as temp_name:
            with patch.object(pipeline, "GENERATED", Path(temp_name)):
                with patch("google.cloud.texttospeech.TextToSpeechClient", return_value=fake_client):
                    pipeline.synthesize_tts(sections, "2026-09-16")
            item = sections[0]["news_items"][0]
            self.assertTrue((Path(temp_name) / item["audio_path"]).exists())
        self.assertEqual(["cmn-CN-Chirp3-HD-Achernar", "cmn-CN-Wavenet-A"], fake_client.voices)
        self.assertEqual("cmn-CN-Wavenet-A", item["audio_voice"])
        self.assertTrue(item["audio_fallback_used"])

    def test_synthesize_tts_skips_article_after_primary_and_fallback_retries_are_exhausted(self):
        from google.api_core import exceptions as google_exceptions

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def synthesize_speech(self, input, voice, audio_config):
                self.calls += 1
                raise google_exceptions.ServiceUnavailable("temporary outage")

        fake_client = FakeClient()
        sections = [{"topic": "technology", "news_items": [{"id": "skip", "tts_text": "失败测试。" * 20}]}]
        with tempfile.TemporaryDirectory() as temp_name:
            with patch.object(pipeline, "GENERATED", Path(temp_name)):
                with patch.object(pipeline, "TTS_MAX_ATTEMPTS", 2):
                    with patch.object(pipeline.time, "sleep"):
                        with patch("google.cloud.texttospeech.TextToSpeechClient", return_value=fake_client):
                            pipeline.synthesize_tts(sections, "2026-09-16")
        item = sections[0]["news_items"][0]
        self.assertEqual("", item["audio_path"])
        self.assertIn("ServiceUnavailable", item["audio_error"])
        self.assertEqual(4, fake_client.calls)

    def test_synthesize_tts_skips_article_on_invalid_argument(self):
        from google.api_core import exceptions as google_exceptions

        class FakeClient:
            def synthesize_speech(self, input, voice, audio_config):
                raise google_exceptions.InvalidArgument("sentence is too long")

        sections = [{"topic": "technology", "news_items": [{"id": "invalid", "tts_text": "无效句子。" * 20}]}]
        with tempfile.TemporaryDirectory() as temp_name:
            with patch.object(pipeline, "GENERATED", Path(temp_name)):
                with patch("google.cloud.texttospeech.TextToSpeechClient", return_value=FakeClient()):
                    pipeline.synthesize_tts(sections, "2026-09-16")
        item = sections[0]["news_items"][0]
        self.assertEqual("", item["audio_path"])
        self.assertIn("InvalidArgument", item["audio_error"])


if __name__ == "__main__":
    unittest.main()
