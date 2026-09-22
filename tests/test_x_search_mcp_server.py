import io
import json
import os
import ssl
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "x-search"
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import x_search_mcp_server as server  # noqa: E402


class PluginPackageTests(unittest.TestCase):
    def test_mcp_server_config_launches_from_plugin_root(self):
        config = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
        server_config = config["mcpServers"]["x-search"]

        self.assertEqual(server_config["command"], "node")
        self.assertEqual(server_config["args"], ["scripts/x_search_mcp_launcher.js"])
        self.assertEqual(server_config["cwd"], ".")
        self.assertEqual(server_config["env"], {})

    def test_mcp_launcher_is_packaged_and_checks_common_python_names(self):
        launcher_path = PLUGIN_ROOT / "scripts" / "x_search_mcp_launcher.js"
        launcher = launcher_path.read_text(encoding="utf-8")

        self.assertTrue(launcher_path.is_file())
        self.assertIn('"python3"', launcher)
        self.assertIn('"python"', launcher)
        self.assertIn('"py"', launcher)
        self.assertIn("x_search_mcp_server.py", launcher)


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        del size
        return json.dumps(self.payload).encode("utf-8")


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, payload):
        self._body = json.dumps(payload).encode("utf-8")
        self._reader = io.BytesIO(self._body)
        super().__init__(
            url="https://api.x.ai/v1/responses",
            code=code,
            msg="HTTP error",
            hdrs={},
            fp=None,
        )

    def read(self, size=-1):
        return self._reader.read(size)


class XSearchToolTests(unittest.TestCase):
    def test_builds_xai_responses_payload(self):
        captured = {}

        def fake_post(url, headers, payload, timeout_seconds):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = payload
            captured["timeout_seconds"] = timeout_seconds
            return {
                "output_text": "answer",
                "citations": [{"url": "https://x.com/xai/status/1"}],
            }

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {
                "X_SEARCH_HOME": home,
                "XAI_API_KEY": "key",
            },
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"model": "grok-test", "timeout_seconds": "31", "retries": "0"},
        ), mock.patch.object(server, "_post_json", side_effect=fake_post):
            result = server.x_search_tool(
                {
                    "query": "latest from xai",
                    "allowed_x_handles": ["@xai"],
                    "from_date": "2026-05-01",
                    "to_date": "2026-05-31",
                    "enable_image_understanding": True,
                    "enable_video_understanding": True,
                }
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["credential_source"], "xai")
        self.assertEqual(captured["url"], "https://api.x.ai/v1/responses")
        self.assertAlmostEqual(captured["timeout_seconds"], 31, delta=0.1)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer key")
        self.assertEqual(
            captured["payload"],
            {
                "model": "grok-test",
                "input": [{"role": "user", "content": "latest from xai"}],
                "tools": [
                    {
                        "type": "x_search",
                        "allowed_x_handles": ["xai"],
                        "from_date": "2026-05-01",
                        "to_date": "2026-05-31",
                        "enable_image_understanding": True,
                        "enable_video_understanding": True,
                    }
                ],
                "store": False,
            },
        )

    def test_marks_degraded_for_filtered_uncited_result(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {
                "X_SEARCH_HOME": home,
                "XAI_API_KEY": "key",
            },
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", return_value={"output_text": "answer"}):
            result = server.x_search_tool({"query": "anything", "allowed_x_handles": ["ghost"]})

        self.assertTrue(result["success"])
        self.assertTrue(result["degraded"])
        self.assertEqual(
            result["degraded_reason"],
            "no citations returned despite filters: allowed_x_handles",
        )

    def test_marks_degraded_for_excluded_handle_filter(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", return_value={"output_text": "answer", "citations": []}):
            result = server.x_search_tool(
                {"query": "anything", "excluded_x_handles": ["noisy"]}
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["degraded"])
        self.assertIn("excluded_x_handles", result["degraded_reason"])

    def test_marks_degraded_for_date_range_filter(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", return_value={"output_text": "answer", "citations": []}):
            result = server.x_search_tool(
                {
                    "query": "anything",
                    "from_date": "2026-05-01",
                    "to_date": "2026-05-02",
                }
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["degraded"])
        self.assertIn("from_date", result["degraded_reason"])
        self.assertIn("to_date", result["degraded_reason"])

    def test_latest_author_query_gets_precision_hint_without_handle_filter(self):
        captured = {}

        def fake_post(url, headers, payload, timeout_seconds):
            del url, headers, timeout_seconds
            captured["payload"] = payload
            return {
                "output_text": "answer",
                "citations": [{"url": "https://x.com/VioletsRiy/status/1"}],
            }

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", side_effect=fake_post):
            result = server.x_search_tool({"query": "وش آخر تغريدة نزلها رياض محمد نبيل"})

        self.assertTrue(result["success"])
        self.assertEqual(result["query_strategy"], "latest_author_post")
        self.assertIn("official or verified X account", captured["payload"]["input"][0]["content"])

    def test_latest_author_query_skips_precision_hint_with_handle_filter(self):
        captured = {}

        def fake_post(url, headers, payload, timeout_seconds):
            del url, headers, timeout_seconds
            captured["payload"] = payload
            return {"output_text": "answer", "citations": []}

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", side_effect=fake_post):
            result = server.x_search_tool(
                {
                    "query": "وش آخر تغريدة نزلها رياض محمد نبيل",
                    "allowed_x_handles": ["VioletsRiy"],
                }
            )

        self.assertTrue(result["success"])
        self.assertIsNone(result["query_strategy"])
        self.assertEqual(
            captured["payload"]["input"],
            [{"role": "user", "content": "وش آخر تغريدة نزلها رياض محمد نبيل"}],
        )

    def test_latest_topic_query_skips_author_precision_hint(self):
        captured = {}

        def fake_post(url, headers, payload, timeout_seconds):
            del url, headers, timeout_seconds
            captured["payload"] = payload
            return {"output_text": "answer", "citations": []}

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", side_effect=fake_post):
            result = server.x_search_tool({"query": "recent posts about xAI"})

        self.assertTrue(result["success"])
        self.assertIsNone(result["query_strategy"])
        self.assertEqual(
            captured["payload"]["input"],
            [{"role": "user", "content": "recent posts about xAI"}],
        )

    def test_uncited_broad_result_is_degraded_but_inline_citations_count(self):
        inline_payload = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Real post.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://x.com/xai/status/1",
                                    "title": "xAI",
                                    "start_index": 0,
                                    "end_index": 4,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", side_effect=[{"output_text": "broad", "citations": []}, inline_payload]):
            broad = server.x_search_tool({"query": "anything"})
            filtered = server.x_search_tool({"query": "anything", "allowed_x_handles": ["xai"]})

        self.assertTrue(broad["degraded"])
        self.assertEqual(broad["evidence"], "uncited")
        self.assertFalse(filtered["degraded"])
        self.assertIsNone(filtered["degraded_reason"])
        self.assertEqual(filtered["inline_citations"][0]["url"], "https://x.com/xai/status/1")

    def test_not_degraded_with_top_level_citation(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(
            server,
            "_post_json",
            return_value={
                "output_text": "answer",
                "citations": [{"url": "https://x.com/xai/status/1"}],
            },
        ):
            result = server.x_search_tool(
                {"query": "anything", "allowed_x_handles": ["xai"]}
            )

        self.assertTrue(result["success"])
        self.assertFalse(result["degraded"])
        self.assertIsNone(result["degraded_reason"])

    def test_allows_future_to_date(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", return_value={"output_text": "answer", "citations": []}):
            result = server.x_search_tool(
                {
                    "query": "anything",
                    "from_date": "2026-05-30",
                    "to_date": "2999-01-01",
                }
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["answer"], "answer")

    def test_rejects_conflicting_handle_filters(self):
        with mock.patch.object(server, "_resolve_xai_credentials") as resolve_credentials:
            with self.assertRaises(server.XSearchError) as ctx:
                server.x_search_tool(
                    {
                        "query": "anything",
                        "allowed_x_handles": ["xai"],
                        "excluded_x_handles": ["openai"],
                    }
                )
        self.assertIn("cannot be used together", str(ctx.exception))
        resolve_credentials.assert_not_called()

    def test_rejects_future_from_date_before_http_call(self):
        with mock.patch.object(server, "_post_json") as post_json, mock.patch.object(
            server,
            "_resolve_xai_credentials",
        ) as resolve_credentials:
            with self.assertRaises(server.XSearchError):
                server.x_search_tool({"query": "anything", "from_date": "2999-01-01"})
        post_json.assert_not_called()
        resolve_credentials.assert_not_called()

    def test_rejects_malformed_and_inverted_dates_before_http_call(self):
        invalid_cases = [
            ({"query": "anything", "from_date": "not-a-date"}, "from_date must be YYYY-MM-DD"),
            ({"query": "anything", "to_date": "2026/05/01"}, "to_date must be YYYY-MM-DD"),
            (
                {
                    "query": "anything",
                    "from_date": "2026-05-10",
                    "to_date": "2026-05-01",
                },
                "must be on or before",
            ),
        ]
        for arguments, expected in invalid_cases:
            with self.subTest(arguments=arguments), mock.patch.object(
                server,
                "_post_json",
            ) as post_json, mock.patch.object(
                server,
                "_resolve_xai_credentials",
            ) as resolve_credentials:
                with self.assertRaises(server.XSearchError) as ctx:
                    server.x_search_tool(arguments)
                self.assertIn(expected, str(ctx.exception))
                post_json.assert_not_called()
                resolve_credentials.assert_not_called()

    def test_rejects_string_boolean_before_credentials(self):
        with mock.patch.object(server, "_resolve_xai_credentials") as resolve_credentials:
            with self.assertRaises(server.XSearchError) as ctx:
                server.x_search_tool(
                    {
                        "query": "anything",
                        "enable_video_understanding": "false",
                    }
                )
        self.assertIn("must be a boolean", str(ctx.exception))
        resolve_credentials.assert_not_called()

    def test_rejects_custom_api_key_base_url_by_default(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {
                "X_SEARCH_HOME": home,
                "XAI_API_KEY": "key",
                "XAI_BASE_URL": "https://example.com/v1",
            },
            clear=True,
        ):
            with self.assertRaises(server.XSearchError) as ctx:
                server._resolve_xai_credentials(30)
        self.assertIn("non-xAI base URL", str(ctx.exception))

    def test_search_requires_auth_without_starting_login(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ), mock.patch.object(server, "_run_xai_oauth_login") as login:
            with self.assertRaises(server.XSearchNoCredentialsError) as ctx:
                server.x_search_tool({"query": "anything"})

        self.assertIn("x_search_auth", str(ctx.exception))
        login.assert_not_called()

    def test_retries_5xx_then_succeeds(self):
        calls = {"count": 0}

        def fake_post(url, headers, payload, timeout_seconds):
            del url, headers, payload, timeout_seconds
            calls["count"] += 1
            if calls["count"] == 1:
                raise FakeHTTPError(500, {"code": "server_error", "error": "temporary"})
            return {"output_text": "Recovered", "citations": []}

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "1"},
        ), mock.patch.object(server, "_post_json", side_effect=fake_post), mock.patch.object(
            server.time,
            "sleep",
        ) as sleep:
            result = server.x_search_tool({"query": "anything"})

        self.assertTrue(result["success"])
        self.assertEqual(result["answer"], "Recovered")
        self.assertEqual(calls["count"], 2)
        sleep.assert_called_once()

    def test_retries_timeout_then_succeeds(self):
        calls = {"count": 0}

        def fake_post(url, headers, payload, timeout_seconds):
            del url, headers, payload, timeout_seconds
            calls["count"] += 1
            if calls["count"] == 1:
                raise TimeoutError("timed out")
            return {"output_text": "Recovered", "citations": []}

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "1"},
        ), mock.patch.object(server, "_post_json", side_effect=fake_post), mock.patch.object(
            server.time,
            "sleep",
        ) as sleep:
            result = server.x_search_tool({"query": "anything"})

        self.assertTrue(result["success"])
        self.assertEqual(result["answer"], "Recovered")
        self.assertEqual(calls["count"], 2)
        sleep.assert_called_once()


class OAuthResolutionTests(unittest.TestCase):
    def test_callback_cors_origin_returns_only_fixed_origins(self):
        self.assertEqual(
            server._callback_cors_origin("https://accounts.x.ai"),
            "https://accounts.x.ai",
        )
        self.assertEqual(
            server._callback_cors_origin("https://auth.x.ai"),
            "https://auth.x.ai",
        )
        self.assertEqual(server._callback_cors_origin("https://evil.example"), "")
        self.assertEqual(
            server._callback_cors_origin("https://accounts.x.ai\r\nX-Test: injected"),
            "",
        )

    def test_oauth_login_logs_only_local_start_url(self):
        class FakeServer:
            server_address = ("127.0.0.1", 56121)

            def shutdown(self):
                pass

            def server_close(self):
                pass

        class FakeThread:
            def join(self, timeout=None):
                del timeout

        callback_result = {}
        stderr = io.StringIO()
        start_uri = "http://127.0.0.1:56121/start"

        def fake_wait(server_arg, thread_arg, result_arg, timeout_seconds):
            self.assertIs(result_arg, callback_result)
            self.assertEqual(timeout_seconds, 45)
            return {"code": "auth-code", "state": "state-token"}

        with mock.patch.object(
            server,
            "_oauth_discovery",
            return_value={
                "authorization_endpoint": "https://auth.x.ai/oauth2/authorize",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
        ), mock.patch.object(
            server,
            "_start_callback_server",
            return_value=(
                FakeServer(),
                FakeThread(),
                callback_result,
                "http://127.0.0.1:56121/callback",
            ),
        ), mock.patch.object(
            server.uuid,
            "uuid4",
            side_effect=[mock.Mock(hex="state-token"), mock.Mock(hex="nonce-token")],
        ), mock.patch.object(
            server.webbrowser,
            "open",
            return_value=True,
        ) as browser_open, mock.patch.object(
            server,
            "_wait_for_callback",
            side_effect=fake_wait,
        ), mock.patch.object(
            server,
            "_exchange_code_for_tokens",
            return_value={"access_token": "access", "refresh_token": "refresh"},
        ), mock.patch.object(
            server,
            "_store_oauth_payload",
            return_value={"base_url": "https://api.x.ai/v1"},
        ), mock.patch.object(sys, "stderr", stderr):
            result = server._run_xai_oauth_login(timeout_seconds=45, open_browser=True)

        self.assertEqual(result["base_url"], "https://api.x.ai/v1")
        browser_open.assert_called_once_with(start_uri)
        self.assertIn("state-token", callback_result["authorize_url"])
        self.assertIn(start_uri, stderr.getvalue())
        self.assertNotIn("state-token", stderr.getvalue())
        self.assertNotIn("nonce-token", stderr.getvalue())
        self.assertNotIn("https://auth.x.ai/oauth2/authorize", stderr.getvalue())

    def test_cli_auth_output_excludes_tool_credentials(self):
        stdout = io.StringIO()

        with mock.patch.object(
            server,
            "x_search_auth_tool",
            return_value={
                "success": True,
                "provider": "xai",
                "tool": "x_search_auth",
                "authenticated": True,
                "access_token": "secret-access",
                "refresh_token": "secret-refresh",
                "message": "ok",
            },
        ), mock.patch.object(sys, "stdout", stdout):
            exit_code = server._run_cli_auth(["--no-browser"])

        self.assertEqual(exit_code, 0)
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["message"], "X Search authentication completed.")
        self.assertNotIn("access_token", stdout.getvalue())
        self.assertNotIn("refresh_token", stdout.getvalue())

    def test_refresh_discovers_and_persists_missing_token_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_home = Path(tmp)
            auth_path = auth_home / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "provider": "xai",
                        "auth_mode": "oauth_pkce",
                        "tokens": {
                            "access_token": "expired.token.value",
                            "refresh_token": "refresh",
                        },
                        "discovery": {},
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {"X_SEARCH_HOME": str(auth_home)},
                clear=True,
            ), mock.patch.object(
                server,
                "_oauth_discovery",
                return_value={"token_endpoint": "https://auth.x.ai/oauth2/token"},
            ), mock.patch.object(
                server,
                "_jwt_is_expiring",
                return_value=True,
            ), mock.patch.object(
                server,
                "_refresh_oauth_token",
                return_value={"access_token": "fresh", "refresh_token": "refresh2"},
            ):
                token, base_url, source = server._resolve_oauth_credentials(30)

            self.assertEqual((token, base_url, source), ("fresh", "https://api.x.ai/v1", "xai-oauth"))
            saved = json.loads(auth_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["tokens"]["access_token"], "fresh")
            self.assertEqual(saved["tokens"]["refresh_token"], "refresh2")
            self.assertEqual(
                saved["discovery"]["token_endpoint"],
                "https://auth.x.ai/oauth2/token",
            )

    def test_exchange_code_includes_pkce_challenge_echo(self):
        captured = {}

        def fake_urlopen(request, timeout, context=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data.decode("utf-8")
            captured["timeout"] = timeout
            captured["context"] = context
            return FakeHTTPResponse(
                {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            )

        with mock.patch.object(server.urllib.request, "urlopen", side_effect=fake_urlopen):
            payload = server._exchange_code_for_tokens(
                token_endpoint="https://auth.x.ai/oauth2/token",
                code="code",
                redirect_uri="http://127.0.0.1:56121/callback",
                code_verifier="verifier",
                code_challenge="challenge",
                timeout_seconds=45,
            )

        self.assertEqual(payload["access_token"], "access")
        self.assertEqual(captured["url"], "https://auth.x.ai/oauth2/token")
        self.assertIsInstance(captured["context"], ssl.SSLContext)
        parsed = urllib.parse.parse_qs(captured["body"])
        self.assertEqual(parsed["grant_type"], ["authorization_code"])
        self.assertEqual(parsed["code_verifier"], ["verifier"])
        self.assertEqual(parsed["code_challenge"], ["challenge"])
        self.assertEqual(parsed["code_challenge_method"], ["S256"])

    def test_auth_tool_reports_existing_api_key_without_browser(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(server, "_run_xai_oauth_login") as login:
            result = server.x_search_auth_tool({})

        self.assertTrue(result["success"])
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["credential_source"], "xai")
        self.assertNotIn("permission_required", result)
        login.assert_not_called()

    def test_auth_tool_requires_permission_before_opening_browser(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ), mock.patch.object(server, "_run_xai_oauth_login") as login:
            result = server.x_search_auth_tool({})

        self.assertTrue(result["success"])
        self.assertFalse(result["authenticated"])
        self.assertTrue(result["permission_required"])
        self.assertEqual(result["permission_prompt"], server.X_SEARCH_AUTH_PERMISSION_MESSAGE)
        self.assertEqual(result["allow_tool"], "x_search_auth")
        self.assertTrue(result["allow_arguments"]["allow_redirect"])
        login.assert_not_called()

    def test_auth_tool_opens_browser_after_permission(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ), mock.patch.object(
            server,
            "_run_xai_oauth_login",
            return_value={"base_url": "https://api.x.ai/v1"},
        ) as login:
            result = server.x_search_auth_tool(
                {
                    "allow_redirect": True,
                    "open_browser": True,
                    "timeout_seconds": 45,
                }
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["credential_source"], "xai-oauth")
        login.assert_called_once_with(timeout_seconds=45, open_browser=True)

    def test_status_uses_codex_x_search_home(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ):
            result = server.x_search_status_tool({})

        self.assertTrue(result["success"])
        self.assertFalse(result["authenticated"])
        self.assertEqual(result["auth_store"], str(Path(home) / "auth.json"))
        self.assertIn("x_search_auth", result["error"])

    def test_status_refreshes_oauth_before_reporting_authenticated(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_home = Path(tmp)
            auth_path = auth_home / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "provider": "xai",
                        "auth_mode": "oauth_pkce",
                        "tokens": {
                            "access_token": "expired.token.value",
                            "refresh_token": "refresh",
                        },
                        "discovery": {
                            "token_endpoint": "https://auth.x.ai/oauth2/token",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"X_SEARCH_HOME": str(auth_home)},
                clear=True,
            ), mock.patch.object(
                server,
                "_jwt_is_expiring",
                return_value=True,
            ), mock.patch.object(
                server,
                "_refresh_oauth_token",
                return_value={"access_token": "fresh", "refresh_token": "refresh"},
            ):
                result = server.x_search_status_tool({})

        self.assertTrue(result["authenticated"])
        self.assertEqual(result["credential_source"], "xai-oauth")
        self.assertEqual(result["base_url"], "https://api.x.ai/v1")


class HttpTransportTests(unittest.TestCase):
    def test_post_json_uses_https_context(self):
        context = object()
        captured = {}

        def fake_urlopen(request, timeout, context=None):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["context"] = context
            return FakeHTTPResponse({"output_text": "ok"})

        with mock.patch.object(server, "_https_context", return_value=context), mock.patch.object(
            server.urllib.request,
            "urlopen",
            side_effect=fake_urlopen,
        ):
            result = server._post_json(
                "https://api.x.ai/v1/responses",
                {"Accept": "application/json"},
                {"input": "hi"},
                33,
            )

        self.assertEqual(result["output_text"], "ok")
        self.assertEqual(captured["url"], "https://api.x.ai/v1/responses")
        self.assertEqual(captured["timeout"], 33)
        self.assertIs(captured["context"], context)

    def test_tls_certificate_error_is_actionable(self):
        def fake_urlopen(request, timeout, context=None):
            del request, timeout, context
            raise urllib.error.URLError(
                ssl.SSLCertVerificationError("certificate verify failed")
            )

        with mock.patch.object(server.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(server.XSearchError) as ctx:
                server._post_json(
                    "https://api.x.ai/v1/responses",
                    {"Accept": "application/json"},
                    {"input": "hi"},
                    33,
                )

        self.assertIn("TLS certificate verification failed", str(ctx.exception))
        self.assertIn("X_SEARCH_CA_BUNDLE", str(ctx.exception))

    def test_urlopen_retries_with_next_ca_bundle_after_tls_error(self):
        captured_contexts = []

        def fake_urlopen(request, timeout, context=None):
            del request, timeout
            captured_contexts.append(context)
            if len(captured_contexts) == 1:
                raise urllib.error.URLError(
                    ssl.SSLCertVerificationError("certificate verify failed")
                )
            return FakeHTTPResponse({"output_text": "ok"})

        with mock.patch.object(
            server,
            "_ca_bundle_candidates",
            return_value=["/tmp/stale.pem", "/tmp/system.pem"],
        ), mock.patch.object(
            server,
            "_https_context",
            side_effect=lambda cafile=None: f"context:{cafile or 'default'}",
        ), mock.patch.object(
            server.urllib.request,
            "urlopen",
            side_effect=fake_urlopen,
        ):
            response = server._urlopen(
                urllib.request.Request("https://api.x.ai/v1/responses"),
                33,
            )

        self.assertEqual(json.loads(response.read().decode("utf-8"))["output_text"], "ok")
        self.assertEqual(
            captured_contexts,
            ["context:/tmp/stale.pem", "context:/tmp/system.pem"],
        )

    def test_default_config_favors_faster_search(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ):
            config = server._x_search_config()

        self.assertEqual(config["timeout_seconds"], 75)
        self.assertEqual(config["retries"], 1)


class McpProtocolTests(unittest.TestCase):
    def test_mcp_tools_list_exposes_auth_before_search(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ):
            response = server._handle_mcp_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertIn("x_search", names)
        self.assertIn("x_search_auth", names)
        self.assertIn("x_search_status", names)

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ):
            authed_response = server._handle_mcp_request(
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
            )
        authed_names = [tool["name"] for tool in authed_response["result"]["tools"]]
        self.assertIn("x_search", authed_names)
        self.assertIn("x_search_auth", authed_names)

        call_response = server._handle_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "x_search", "arguments": {"query": ""}},
            }
        )
        self.assertTrue(call_response["result"]["isError"])
        body = json.loads(call_response["result"]["content"][0]["text"])
        self.assertEqual(body["error"], "query is required for x_search")

    def test_mcp_search_call_reports_auth_required(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ):
            call_response = server._handle_mcp_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "x_search", "arguments": {"query": "xai"}},
                }
            )
        self.assertTrue(call_response["result"]["isError"])
        body = json.loads(call_response["result"]["content"][0]["text"])
        self.assertTrue(body["auth_required"])
        self.assertEqual(body["auth_tool"], "x_search_auth")
        self.assertTrue(body["permission_required"])
        self.assertEqual(body["permission_prompt"], server.X_SEARCH_AUTH_PERMISSION_MESSAGE)
        self.assertEqual(body["allow_arguments"], {"allow_redirect": True})

    def test_mcp_auth_call_prompts_before_redirect(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ), mock.patch.object(server, "_run_xai_oauth_login") as login:
            call_response = server._handle_mcp_request(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "x_search_auth", "arguments": {}},
                }
            )
        self.assertFalse(call_response["result"]["isError"])
        body = json.loads(call_response["result"]["content"][0]["text"])
        self.assertFalse(body["authenticated"])
        self.assertTrue(body["permission_required"])
        self.assertEqual(body["message"], server.X_SEARCH_AUTH_PERMISSION_MESSAGE)
        login.assert_not_called()

    def test_mcp_content_length_round_trip(self):
        original_stdin = sys.stdin
        original_stdout = sys.stdout
        try:
            message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            body = json.dumps(message).encode("utf-8")
            sys.stdin = io.TextIOWrapper(
                io.BytesIO(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body),
                encoding="utf-8",
            )
            read = server._read_mcp_message()
            self.assertEqual(read, (message, "content-length"))

            out = io.BytesIO()
            sys.stdout = io.TextIOWrapper(out, encoding="utf-8")
            server._send_mcp_message(
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                "content-length",
            )
            sys.stdout.flush()
            self.assertIn(b"Content-Length:", out.getvalue())
        finally:
            sys.stdin = original_stdin
            sys.stdout = original_stdout

    def test_mcp_newline_round_trip(self):
        original_stdin = sys.stdin
        original_stdout = sys.stdout
        try:
            message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            sys.stdin = io.TextIOWrapper(
                io.BytesIO(json.dumps(message).encode("utf-8") + b"\n"),
                encoding="utf-8",
            )
            read = server._read_mcp_message()
            self.assertEqual(read, (message, "newline"))

            out = io.BytesIO()
            sys.stdout = io.TextIOWrapper(out, encoding="utf-8")
            server._send_mcp_message({"jsonrpc": "2.0", "id": 1, "result": {}})
            sys.stdout.flush()
            self.assertTrue(out.getvalue().endswith(b"\n"))
            self.assertNotIn(b"Content-Length:", out.getvalue())
        finally:
            sys.stdin = original_stdin
            sys.stdout = original_stdout

    def test_initialize_selects_supported_protocol_version(self):
        response = server._handle_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1900-01-01"},
            }
        )
        self.assertEqual(
            response["result"]["protocolVersion"],
            server.SUPPORTED_PROTOCOL_VERSIONS[0],
        )

    def test_mcp_rejects_malformed_input(self):
        original_stdin = sys.stdin
        try:
            sys.stdin = io.TextIOWrapper(io.BytesIO(b"not-json\n"), encoding="utf-8")
            with self.assertRaises(server.McpProtocolError) as ctx:
                server._read_mcp_message()
        finally:
            sys.stdin = original_stdin
        self.assertEqual(ctx.exception.code, -32600)


if __name__ == "__main__":
    unittest.main()
