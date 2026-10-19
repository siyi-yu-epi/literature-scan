"""Exercise the scanner through the real SDK with an offline HTTP transport."""

import json
import os
import unittest
from unittest.mock import patch

import httpx
from google import genai
from google.genai import errors
from google.oauth2.credentials import Credentials

from scripts import literature_scan as scanner


ARTICLE = scanner.Article(
    title="Advertising research", authors=["Researcher"], journal="Journal of Marketing",
    url="https://example.org/paper", doi="10.1234/example", published_date="2026-10-19",
    abstract="An experiment on advertising.", source_type="RSS",
)
ANALYSIS = {
    "english_summary": "An advertising experiment.",
    "chinese_summary": "一项广告实验。",
    "keywords": ["Advertising"],
    "relevance_score": 80,
    "relevance_topics": ["Advertising"],
    "digest_reason": "Relevant to advertising research.",
}


class GeminiRetryTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.delays = []
        test_env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("GOOGLE_", "GEMINI_", "GCLOUD_"))
        }
        test_env.update({
            "GOOGLE_CLOUD_PROJECT": "offline-test-project",
            "GOOGLE_CLOUD_LOCATION": "global",
            "GEMINI_MODEL": "gemini-3.1-pro-preview",
        })
        self.enterContext(patch.dict(os.environ, test_env, clear=True))
        # Skip real waiting, but retain the SDK's retry decisions and wait calculation.
        self.enterContext(patch("time.sleep", side_effect=self.delays.append))

    def client_with_responses(self, statuses, payload=ANALYSIS):
        statuses = iter(statuses)

        def respond(request):
            self.requests.append(request)
            status = next(statuses)
            if status != 200:
                return httpx.Response(status, json={"error": {
                    "code": status,
                    "status": "RESOURCE_EXHAUSTED" if status == 429 else "API_ERROR",
                    "message": "Offline simulated API error",
                }})
            return httpx.Response(200, json={"candidates": [{
                "content": {"role": "model", "parts": [{"text": json.dumps(payload)}]},
                "finishReason": "STOP",
            }]})

        # Inject only transport and credentials; use the application's real client
        # options, SDK request serialization, error classes, and retry implementation.
        real_client = genai.Client
        transport_client = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(transport_client.close)

        def make_client(**kwargs):
            kwargs["credentials"] = Credentials(token="offline-test-token")
            kwargs["http_options"].httpx_client = transport_client
            return real_client(**kwargs)

        with patch.object(genai, "Client", side_effect=make_client):
            client = scanner.gemini_client()
        self.addCleanup(client.close)
        return client

    def test_article_recovers_after_a_sustained_429_burst(self):
        client = self.client_with_responses([429] * 5 + [200])
        try:
            result = scanner.analyze_article(client, ARTICLE)
        except errors.APIError as exc:
            self.fail(f"Transient 429 aborted the article after {len(self.requests)} request(s): {exc}")
        self.assertEqual(result.english_summary, "An advertising experiment.")
        self.assertEqual(result.relevance_score, 80)
        self.assertEqual(len(self.requests), 6)
        self.assertGreaterEqual(sum(self.delays), 345)
        self.assertTrue(all(0 < delay <= 120 for delay in self.delays))
        self.assertEqual({request.content for request in self.requests}, {self.requests[0].content})
        self.assertEqual(self.requests[0].url.host, "aiplatform.googleapis.com")

    def test_weekly_summary_also_recovers_from_429(self):
        client = self.client_with_responses([429, 200], {
            "english_trend": "Advertising is a theme.", "chinese_trend": "广告是一个主题。",
        })
        items = [scanner.CreatedArticle(ARTICLE, scanner.ArticleAnalysis(
            "An advertising experiment.", "一项广告实验。", [], 80, ["Advertising"], "Relevant.",
        ))]
        try:
            result = scanner.summarize_weekly_trend(client, items)
        except errors.APIError as exc:
            self.fail(f"Transient 429 aborted the weekly summary: {exc}")
        self.assertEqual(result, ("Advertising is a theme.", "广告是一个主题。"))
        self.assertEqual(len(self.requests), 2)

    def test_persistent_429_stops_with_original_error_after_eight_attempts(self):
        client = self.client_with_responses([429] * 20)
        with self.assertRaises(errors.ClientError) as caught:
            scanner.analyze_article(client, ARTICLE)
        self.assertEqual(caught.exception.code, 429)
        self.assertEqual(len(self.requests), 8)
        self.assertEqual(len(self.delays), 7)
        self.assertGreaterEqual(sum(self.delays), 585)
        self.assertTrue(all(0 < delay <= 120 for delay in self.delays))

    def test_temporary_service_failure_is_retried(self):
        client = self.client_with_responses([503, 200])
        try:
            result = scanner.analyze_article(client, ARTICLE)
        except errors.APIError as exc:
            self.fail(f"Transient 503 aborted the article: {exc}")
        self.assertEqual(result.relevance_score, 80)
        self.assertEqual(len(self.requests), 2)

    def test_authentication_and_invalid_request_errors_fail_immediately(self):
        for code in (400, 401, 403, 404):
            with self.subTest(code=code):
                self.requests.clear()
                self.delays.clear()
                client = self.client_with_responses([code, 200])
                with self.assertRaises(errors.ClientError) as caught:
                    scanner.analyze_article(client, ARTICLE)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.delays, [])


if __name__ == "__main__":
    unittest.main()
