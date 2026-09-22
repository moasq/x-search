import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "x-search"
sys.path.insert(0, str(PLUGIN / "scripts"))
import x_search_mcp_server as server
from x_search_research import collect_sources, research_arguments, source_warnings, x_source

POST = "https://x.com/NASA/status/2102147059411263495"
CITED = {"output_text": "A cited answer", "citations": [POST], "status": "completed"}


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://api.x.ai/v1/responses", code, "error", headers or {},
                                  io.BytesIO(b'{"error":"try later"}'))


class ResearchUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = mock.patch.dict(os.environ, {"X_SEARCH_HOME": self.home.name, "XAI_API_KEY": "test-key"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_research_tool_discovery_and_dispatch(self):
        response = server._handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in response["result"]["tools"]}
        self.assertEqual(names, {"x_search", "x_search_account", "x_search_thread", "x_search_auth", "x_search_status", "x_search_logout"})
        with mock.patch.object(server, "_post_json", return_value=CITED):
            response = server._handle_mcp_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "x_search_thread", "arguments": {"url": POST}}})
        self.assertFalse(response["result"]["isError"])
        self.assertTrue(json.loads(response["result"]["content"][0]["text"])["target_cited"])

    def test_account_query_preserves_filters_and_replies(self):
        with mock.patch.object(server, "_post_json", return_value=CITED) as post:
            result = server._call_tool("x_search_account", {"handle": "@NASA", "include_replies": True,
                "from_date": "2026-05-01", "max_posts": 3, "focus": "missions"})
        payload = post.call_args.args[2]
        self.assertEqual(payload["tools"][0]["allowed_x_handles"], ["NASA"])
        self.assertEqual(payload["tools"][0]["from_date"], "2026-05-01")
        self.assertIn("including their replies", payload["input"][0]["content"])
        self.assertIn("at most 3", payload["input"][0]["content"])
        self.assertEqual(result["coverage"], "search_sample")

    def test_thread_requires_requested_root_citation(self):
        with mock.patch.object(server, "_post_json", return_value=CITED):
            result = server._call_tool("x_search_thread", {"url": "https://x.com/NASA/status/123"})
        self.assertFalse(result["success"])
        self.assertFalse(result["target_cited"])

    def test_account_requires_a_post_from_target_not_just_profile_or_other_account(self):
        for citation in ("https://x.com/NASA", "https://x.com/SpaceX/status/123"):
            with self.subTest(citation=citation), mock.patch.object(server, "_post_json", return_value={"output_text": "answer", "citations": [citation]}):
                result = server._call_tool("x_search_account", {"handle": "NASA"})
            self.assertFalse(result["success"])
            self.assertFalse(result["target_cited"])

    def test_research_input_errors_never_resolve_credentials(self):
        invalid = [
            ("account", {"handle": "bad handle"}),
            ("account", {"handle": "NASA", "max_posts": 1.5}),
            ("account", {"handle": "NASA", "include_replies": "false"}),
            ("thread", {"url": "https://x.com.evil.test/NASA/status/1"}),
            ("thread", {"url": "https://x.com/NASA"}),
            ("thread", {"url": POST, "cursor": "fake"}),
        ]
        with mock.patch.object(server, "_resolve_xai_credentials") as auth:
            for kind, args in invalid:
                with self.subTest(args=args), self.assertRaises(server.XSearchError):
                    server.x_search_research_tool(kind, args)
        auth.assert_not_called()

    def test_reasoning_omitted_by_default_and_overridable(self):
        with mock.patch.object(server, "_post_json", return_value=CITED) as post:
            server.x_search_tool({"query": "NASA"})
            self.assertNotIn("reasoning", post.call_args.args[2])
            server.x_search_tool({"query": "NASA", "reasoning_effort": "high"})
            self.assertEqual(post.call_args.args[2]["reasoning"], {"effort": "high"})

    def test_invalid_search_inputs_fail_before_auth(self):
        cases = [{"query": ["NASA"]}, {"query": "NASA", "from_date": "2026-5-1"},
                 {"query": "NASA", "reasoning_effort": "max"},
                 {"query": "NASA", "timeout_seconds": 10.9},
                 {"query": "NASA", "require_citations": "true"},
                 {"query": "NASA", "allowed_x_handles": ["NASA/foo"]}]
        with mock.patch.object(server, "_resolve_xai_credentials") as auth:
            for args in cases:
                with self.subTest(args=args), self.assertRaises(server.XSearchError):
                    server.x_search_tool(args)
        auth.assert_not_called()

    def test_supports_twenty_handles_and_deduplicates_case(self):
        handles = [f"user{i}" for i in range(20)]
        with mock.patch.object(server, "_post_json", return_value=CITED) as post:
            server.x_search_tool({"query": "test", "allowed_x_handles": handles})
        self.assertEqual(post.call_args.args[2]["tools"][0]["allowed_x_handles"], handles)
        self.assertEqual(server._normalize_handles(["NASA", "@nasa"], "handles"), ["NASA"])
        with self.assertRaises(server.XSearchError):
            server._normalize_handles(handles + ["extra"], "handles")

    def test_strict_evidence_rejects_empty_foreign_or_incomplete_results(self):
        responses = [{"output_text": "Uncited answer"},
                     {"output_text": "Foreign citation", "citations": ["https://example.com"]},
                     {**CITED, "status": "incomplete"},
                     {"citations": [POST]}]
        for response in responses:
            with self.subTest(response=response), mock.patch.object(server, "_post_json", return_value=response):
                result = server.x_search_tool({"query": "NASA", "require_citations": True})
            self.assertFalse(result["success"])
            self.assertTrue(result["degraded"])
            self.assertEqual(result["error_type"], "InsufficientEvidence")

    def test_usage_and_latency_are_returned_without_request_secrets(self):
        with mock.patch.object(server, "_post_json", return_value={**CITED, "usage": {"total_tokens": 42}}):
            result = server.x_search_tool({"query": "NASA"})
        self.assertEqual(result["usage"]["total_tokens"], 42)
        self.assertEqual(result["diagnostics"]["attempts"], 1)
        self.assertNotIn("test-key", json.dumps(result))

    def test_rate_limit_retry_honors_retry_after(self):
        with mock.patch.object(server, "_post_json", side_effect=[http_error(429, {"Retry-After": "4"}), CITED]) as post, mock.patch.object(server.time, "sleep") as sleep:
            result = server.x_search_tool({"query": "NASA"})
        self.assertTrue(result["success"])
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(4.0)

    def test_long_retry_after_returns_without_sleeping(self):
        with mock.patch.object(server, "_post_json", side_effect=http_error(429, {"Retry-After": "120"})) as post, mock.patch.object(server.time, "sleep") as sleep:
            result = server.x_search_tool({"query": "NASA", "timeout_seconds": 10})
        self.assertFalse(result["success"])
        self.assertEqual(result["retry_after_seconds"], 120)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_retry_budget_shrinks_after_first_attempt(self):
        clock = [0.0]
        timeouts = []
        def post(url, headers, payload, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                clock[0] += 6
                raise TimeoutError("socket timed out")
            return CITED
        with mock.patch.object(server.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(server.time, "sleep", side_effect=lambda n: clock.__setitem__(0, clock[0]+n)), mock.patch.object(server, "_post_json", side_effect=post):
            result = server.x_search_tool({"query": "NASA", "timeout_seconds": 10})
        self.assertTrue(result["success"])
        self.assertEqual(timeouts, [10, 2.5])

    def test_oauth_401_refresh_gets_replay_even_with_zero_retries(self):
        with mock.patch.dict(os.environ, {"X_SEARCH_RETRIES": "0"}), mock.patch.object(server, "_resolve_xai_credentials", side_effect=[("old", server.DEFAULT_XAI_BASE_URL, "xai-oauth"), ("fresh", server.DEFAULT_XAI_BASE_URL, "xai-oauth")]) as auth, mock.patch.object(server, "_post_json", side_effect=[http_error(401), CITED]) as post:
            result = server.x_search_tool({"query": "NASA"})
        self.assertTrue(result["success"])
        self.assertEqual(post.call_count, 2)
        self.assertTrue(auth.call_args.kwargs["force_refresh"])

    def test_repeated_401_stops_without_looping(self):
        with mock.patch.object(server, "_resolve_xai_credentials", return_value=("token", server.DEFAULT_XAI_BASE_URL, "xai-oauth")), mock.patch.object(server, "_post_json", side_effect=[http_error(401), http_error(401)]) as post:
            result = server.x_search_tool({"query": "NASA"})
        self.assertFalse(result["success"])
        self.assertEqual(post.call_count, 2)

    def test_oauth_stays_default_and_api_key_preference_is_explicit(self):
        with mock.patch.object(server, "_resolve_oauth_credentials", return_value=("oauth", server.DEFAULT_XAI_BASE_URL, "xai-oauth")) as oauth:
            self.assertEqual(server._resolve_xai_credentials(30)[2], "xai-oauth")
            oauth.reset_mock()
            with mock.patch.dict(os.environ, {"X_SEARCH_CREDENTIAL_PREFERENCE": "api_key"}):
                self.assertEqual(server._resolve_xai_credentials(30)[2], "xai")
            oauth.assert_not_called()

    def test_status_reports_effective_config_without_secrets(self):
        status = server.x_search_status_tool({})
        self.assertEqual(status["server_version"], "0.3.0")
        self.assertEqual(status["config"]["credential_preference"], "oauth")
        self.assertNotIn("test-key", json.dumps(status))


class CitationTests(unittest.TestCase):
    def test_normalizes_deduplicates_and_labels_metadata(self):
        source = collect_sources([POST, {"url": POST.replace("x.com", "twitter.com") + "?s=20"}], [{"url": POST}])
        self.assertEqual(len(source), 1)
        self.assertEqual(source[0]["post_id"], "2102147059411263495")
        self.assertEqual(source[0]["citation_channels"], ["citations", "inline_citations"])
        self.assertEqual(source[0]["timestamp_source"], "post_id")
        self.assertFalse(source[0]["content_verified"])

    def test_rejects_deceptive_hosts_and_nonpost_paths(self):
        for url in ("https://x.com.evil.test/NASA/status/1", "https://x.com@evil.test/NASA/status/1", "https://evil.test@x.com/NASA/status/1", "javascript:alert(1)", "https://x.com/search", "https://x.com/NASA/status/999999999999999999999999"):
            with self.subTest(url=url):
                self.assertIsNone(x_source(url))

    def test_warns_on_out_of_window_and_wrong_handle(self):
        sources = collect_sources([POST], [])
        warnings = source_warnings(sources, ["xai"], [], "2020-01-01", "2020-01-02")
        self.assertEqual(len(warnings), 2)

    def test_generated_answer_link_alone_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"X_SEARCH_HOME": home, "XAI_API_KEY": "test-key"}, clear=True), mock.patch.object(server, "_post_json", return_value={"output_text": f"Look at [this post]({POST})"}):
            result = server.x_search_tool({"query": "NASA", "require_citations": True})
        self.assertFalse(result["success"])
        self.assertEqual(result["sources"], [])


class StdioTests(unittest.TestCase):
    def test_real_python_process_lists_six_tools(self):
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        completed = subprocess.run([sys.executable, "scripts/x_search_mcp_server.py"], cwd=PLUGIN,
            input=request, text=True, capture_output=True, timeout=10, check=True)
        response = json.loads(completed.stdout)
        self.assertEqual(len(response["result"]["tools"]), 6)


if __name__ == "__main__":
    unittest.main()
