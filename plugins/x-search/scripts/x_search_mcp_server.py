#!/usr/bin/env python3
"""Standalone MCP server for xAI-backed X Search in Codex.

The server exposes an `x_search` tool that calls xAI's Responses API with the
server-side {"type": "x_search"} tool. It also owns its own browser-based xAI
OAuth PKCE sign-in flow, stores tokens in the current user's X Search config
directory, and never depends on any external agent runtime.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import os
import re
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from x_search_research import (
    REASONING_EFFORTS, collect_sources, normalize_handle, research_arguments,
    research_schema, source_warnings,
)


DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_XAI_OAUTH_BASE_URL = "https://api.x.ai/v1"
XAI_OAUTH_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_REDIRECT_HOST = "127.0.0.1"
XAI_OAUTH_REDIRECT_PORT = 56121
XAI_OAUTH_REDIRECT_PATH = "/callback"
XAI_OAUTH_START_PATH = "/start"
DEFAULT_X_SEARCH_MODEL = "grok-4.20-reasoning"
DEFAULT_X_SEARCH_TIMEOUT_SECONDS = 75
DEFAULT_X_SEARCH_RETRIES = 1
DEFAULT_CA_BUNDLE_PATHS = (
    "/etc/ssl/cert.pem",
    "/opt/homebrew/etc/ca-certificates/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
)
XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120
MAX_HANDLES = 20
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_ERROR_BYTES = 16 * 1024
MAX_MCP_CONTENT_LENGTH = 5 * 1024 * 1024
SERVER_NAME = "x-search"
SERVER_VERSION = "0.3.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2024-11-05")


X_SEARCH_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to look up on X.",
        },
        "allowed_x_handles": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_HANDLES,
            "description": "Optional list of X handles to include exclusively (max 20).",
        },
        "excluded_x_handles": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_HANDLES,
            "description": "Optional list of X handles to exclude (max 20).",
        },
        "from_date": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "description": "Optional start date in YYYY-MM-DD format.",
        },
        "to_date": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "description": "Optional end date in YYYY-MM-DD format.",
        },
        "enable_image_understanding": {
            "type": "boolean",
            "description": "Whether xAI should analyze images attached to matching X posts.",
            "default": False,
        },
        "enable_video_understanding": {
            "type": "boolean",
            "description": "Whether xAI should analyze videos attached to matching X posts.",
            "default": False,
        },
        "reasoning_effort": {
            "type": "string", "enum": list(REASONING_EFFORTS),
            "description": "Optional reasoning effort; use only with a model that supports it.",
        },
        "timeout_seconds": {
            "type": "integer", "minimum": 10, "maximum": 300,
            "description": "Search retry budget in seconds. Attempts use the remaining time as their socket timeout.",
        },
        "require_citations": {
            "type": "boolean", "default": False,
            "description": "Return a tool error if no X citation is supplied or the response is incomplete.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
    "not": {"required": ["allowed_x_handles", "excluded_x_handles"]},
}


X_SEARCH_AUTH_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "allow_redirect": {
            "type": "boolean",
            "description": (
                "Explicit user permission to open the xAI authorization page. "
                "When false and no credential exists, the tool returns a "
                "permission prompt instead of opening a browser."
            ),
            "default": False,
        },
        "open_browser": {
            "type": "boolean",
            "description": "Open the xAI authorization page in the default browser after permission is granted.",
            "default": True,
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 30,
            "maximum": 900,
            "description": "How long to wait for the local browser callback.",
            "default": 180,
        },
        "force": {
            "type": "boolean",
            "description": "Start a new sign-in even if a credential already exists.",
            "default": False,
        },
    },
    "additionalProperties": False,
}


X_SEARCH_STATUS_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


X_SEARCH_LOGOUT_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


X_SEARCH_TOOL: Dict[str, Any] = {
    "name": "x_search",
    "description": (
        "Search X posts, profiles, and threads using xAI's built-in x_search "
        "Responses tool. Use this for current discussion, reactions, or claims "
        "on X rather than general web pages. Returns a search sample with citation "
        "metadata, not raw timelines. Requires configured xAI OAuth or an API key."
    ),
    "inputSchema": X_SEARCH_INPUT_SCHEMA,
}

RESEARCH_TOOLS = [
    {
        "name": f"x_search_{kind}",
        "description": description,
        "inputSchema": research_schema(kind, X_SEARCH_INPUT_SCHEMA["properties"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True},
    }
    for kind, description in (
        ("account", "Research one public X account's recent posts and themes with citations. Returns a search sample, not a complete or authenticated timeline. Citation evidence is required by default."),
        ("thread", "Research a public X post, author continuations, and replies by exact URL. Returns a cited search sample, not an exhaustive reply export. Citation evidence is required by default."),
    )
]
X_SEARCH_TOOL["annotations"] = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}


X_SEARCH_AUTH_TOOL: Dict[str, Any] = {
    "name": "x_search_auth",
    "description": (
        "Sign in to xAI for X Search. Checks existing access first. If no "
        "credential exists, returns a permission prompt unless allow_redirect "
        "is true; after permission, opens the browser, waits for the local "
        "OAuth callback, and stores the resulting token for this user only."
    ),
    "inputSchema": X_SEARCH_AUTH_INPUT_SCHEMA,
}


X_SEARCH_STATUS_TOOL: Dict[str, Any] = {
    "name": "x_search_status",
    "description": "Check whether X Search has a local xAI credential configured.",
    "inputSchema": X_SEARCH_STATUS_INPUT_SCHEMA,
}


X_SEARCH_LOGOUT_TOOL: Dict[str, Any] = {
    "name": "x_search_logout",
    "description": "Remove this user's locally stored xAI OAuth token for X Search.",
    "inputSchema": X_SEARCH_LOGOUT_INPUT_SCHEMA,
}


X_SEARCH_AUTH_PERMISSION_MESSAGE = (
    "X Search needs to open the xAI authentication page to complete sign-in. "
    "Do you want to allow this?"
)


SETUP_TOOLS = [
    X_SEARCH_AUTH_TOOL,
    X_SEARCH_STATUS_TOOL,
    X_SEARCH_LOGOUT_TOOL,
]


class XSearchError(Exception):
    """Expected X Search error surfaced as a structured tool result."""


class XSearchNoCredentialsError(XSearchError):
    """Raised when xAI credentials are absent or require a fresh sign-in."""


class McpProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _home() -> Path:
    return Path.home()


def _x_search_home() -> Path:
    configured = os.environ.get("X_SEARCH_HOME", "").strip()
    return Path(configured or (_home() / ".codex-x-search")).expanduser()


def _read_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = _strip_inline_comment(value.strip())
        if not key:
            continue
        if (
            (value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _env_value(name: str, default: Optional[str] = None) -> Optional[str]:
    for path in (_x_search_home() / ".env",):
        value = _read_dotenv(path).get(name)
        if value is not None and str(value).strip():
            return str(value).strip()

    value = os.environ.get(name)
    if value is not None and str(value).strip():
        return str(value).strip()

    return default


def _strip_inline_comment(value: str) -> str:
    quote: Optional[str] = None
    for index, char in enumerate(value):
        if char in {"'", '"'}:
            quote = None if quote == char else char if quote is None else quote
        if char == "#" and quote is None:
            return value[:index].strip()
    return value.strip()


def _read_x_search_config() -> Dict[str, str]:
    path = _x_search_home() / "config.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    config = payload.get("x_search", payload)
    if not isinstance(config, dict):
        return {}
    return {str(key): str(value) for key, value in config.items()}


def _x_search_config() -> Dict[str, Any]:
    cfg = _read_x_search_config()

    model = (
        _env_value("X_SEARCH_MODEL")
        or cfg.get("model")
        or DEFAULT_X_SEARCH_MODEL
    )
    timeout_raw = (
        _env_value("X_SEARCH_TIMEOUT_SECONDS")
        or cfg.get("timeout_seconds")
        or str(DEFAULT_X_SEARCH_TIMEOUT_SECONDS)
    )
    retries_raw = (
        _env_value("X_SEARCH_RETRIES")
        or cfg.get("retries")
        or str(DEFAULT_X_SEARCH_RETRIES)
    )

    try:
        timeout_seconds = min(300, max(10, int(str(timeout_raw))))
    except Exception:
        timeout_seconds = DEFAULT_X_SEARCH_TIMEOUT_SECONDS

    try:
        retries = min(3, max(0, int(str(retries_raw))))
    except Exception:
        retries = DEFAULT_X_SEARCH_RETRIES

    reasoning_effort = str(_env_value("X_SEARCH_REASONING_EFFORT") or cfg.get("reasoning_effort") or "").strip().lower()
    if reasoning_effort and reasoning_effort not in REASONING_EFFORTS:
        raise XSearchError("reasoning_effort must be one of: " + ", ".join(REASONING_EFFORTS))
    credential_preference = str(_env_value("X_SEARCH_CREDENTIAL_PREFERENCE") or cfg.get("credential_preference") or "oauth").strip().lower()
    if credential_preference not in {"oauth", "api_key"}:
        raise XSearchError("credential_preference must be oauth or api_key")
    return {
        "model": str(model).strip() or DEFAULT_X_SEARCH_MODEL,
        "timeout_seconds": timeout_seconds,
        "retries": retries,
        "reasoning_effort": reasoning_effort or None,
        "credential_preference": credential_preference,
    }


def _base64url_json(segment: str) -> Dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def _jwt_is_expiring(access_token: str, skew_seconds: int) -> bool:
    if "." not in access_token:
        return False
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return False
        payload = _base64url_json(parts[1])
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        return float(exp) <= time.time() + max(0, int(skew_seconds))
    except Exception:
        return False


def _load_auth_store() -> Tuple[Path, Dict[str, Any]]:
    path = _x_search_home() / "auth.json"
    if not path.is_file():
        return path, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return path, {}
    return path, payload if isinstance(payload, dict) else {}


@contextlib.contextmanager
def _auth_store_lock():
    lock_path = _x_search_home() / "auth.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl_module = None
        try:
            import fcntl as fcntl_module

            fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_EX)
        except Exception:
            fcntl_module = None
        try:
            yield
        finally:
            if fcntl_module is not None:
                try:
                    fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_UN)
                except Exception:
                    pass


def _save_auth_store(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as handle:
        handle.write(data)
        temp_path = Path(handle.name)
    try:
        os.chmod(temp_path, 0o600)
    except OSError:
        pass
    os.replace(temp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _delete_auth_store() -> bool:
    auth_path = _x_search_home() / "auth.json"
    lock_path = _x_search_home() / "auth.lock"
    removed = False
    for path in (auth_path, lock_path):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return removed


def _xai_endpoint_ok(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "x.ai" or host.endswith(".x.ai"))


def _validate_base_url(base_url: str, credential_source: str) -> str:
    stripped = str(base_url or DEFAULT_XAI_BASE_URL).strip().rstrip("/")
    parsed = urllib.parse.urlparse(stripped)
    if parsed.scheme != "https":
        raise XSearchError("Refusing to send xAI bearer to a non-HTTPS base URL")
    if _xai_endpoint_ok(stripped):
        return stripped
    allow_custom = str(_env_value("X_SEARCH_ALLOW_CUSTOM_BASE_URL", "") or "").lower()
    if credential_source == "xai" and allow_custom in {"1", "true", "yes"}:
        return stripped
    raise XSearchError(
        "Refusing to send xAI bearer to a non-xAI base URL. Set "
        "X_SEARCH_ALLOW_CUSTOM_BASE_URL=1 only if you intentionally use a trusted proxy."
    )


def _read_limited(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise XSearchError(f"HTTP response exceeded {limit} bytes")
    return data


def _read_json_response(response: Any, limit: int = MAX_RESPONSE_BYTES) -> Dict[str, Any]:
    payload = json.loads(_read_limited(response, limit).decode("utf-8"))
    if not isinstance(payload, dict):
        raise XSearchError("HTTP response was not a JSON object")
    return payload


def _append_existing_ca_bundle(candidates: List[Optional[str]], raw_path: str) -> None:
    path = Path(str(raw_path)).expanduser()
    if path.is_file():
        candidates.append(str(path))


def _ca_bundle_candidates() -> List[Optional[str]]:
    configured = str(_env_value("X_SEARCH_CA_BUNDLE") or "").strip()
    candidates: List[Optional[str]] = []
    if configured:
        _append_existing_ca_bundle(candidates, configured)

    for raw_path in DEFAULT_CA_BUNDLE_PATHS:
        _append_existing_ca_bundle(candidates, raw_path)

    try:
        import certifi  # type: ignore

        _append_existing_ca_bundle(candidates, str(certifi.where()))
    except Exception:
        pass

    candidates.append(None)
    unique_candidates: List[Optional[str]] = []
    seen = set()
    for candidate in candidates:
        key = candidate or ""
        if key not in seen:
            seen.add(key)
            unique_candidates.append(candidate)
    return unique_candidates


def _https_context(cafile: Optional[str] = None) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()


def _is_tls_verification_error(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, ssl.SSLCertVerificationError) or (
        isinstance(reason, ssl.SSLError)
        and "CERTIFICATE_VERIFY_FAILED" in str(reason).upper()
    )


def _tls_failure_message(attempted: Sequence[Optional[str]]) -> str:
    attempted_labels = [candidate or "default trust store" for candidate in attempted]
    attempted_text = ", ".join(attempted_labels) if attempted_labels else "default trust store"
    return (
        "TLS certificate verification failed while connecting to xAI after trying "
        f"{attempted_text}. Set X_SEARCH_CA_BUNDLE to a valid CA bundle path, then retry."
    )


def _urlopen(request: urllib.request.Request, timeout_seconds: int) -> Any:
    attempted: List[Optional[str]] = []
    last_tls_error: Optional[BaseException] = None
    for cafile in _ca_bundle_candidates():
        attempted.append(cafile)
        try:
            return urllib.request.urlopen(
                request,
                timeout=timeout_seconds,
                context=_https_context(cafile),
            )
        except (ssl.SSLError, urllib.error.URLError) as exc:
            if _is_tls_verification_error(exc):
                last_tls_error = exc
                continue
            raise
    raise XSearchError(_tls_failure_message(attempted)) from last_tls_error


def _redact(text: str) -> str:
    patterns = [
        r"Bearer\s+[A-Za-z0-9._~+/=-]+",
        r'("?(?:access_token|refresh_token|api_key|authorization)"?\s*[:=]\s*")([^"]+)(")',
        r"((?:access_token|refresh_token|api_key|authorization)\s*[:=]\s*)([^\s,&]+)",
    ]
    redacted = text
    redacted = re.sub(patterns[0], "Bearer [REDACTED]", redacted, flags=re.IGNORECASE)
    redacted = re.sub(patterns[1], r"\1[REDACTED]\3", redacted, flags=re.IGNORECASE)
    redacted = re.sub(patterns[2], r"\1[REDACTED]", redacted, flags=re.IGNORECASE)
    return redacted


def _oauth_discovery(timeout_seconds: int) -> Dict[str, str]:
    request = urllib.request.Request(
        XAI_OAUTH_DISCOVERY_URL,
        headers={"Accept": "application/json", "User-Agent": _user_agent()},
    )
    with _urlopen(request, max(5, timeout_seconds)) as response:
        payload = _read_json_response(response)
    token_endpoint = str(payload.get("token_endpoint") or "").strip()
    authorization_endpoint = str(payload.get("authorization_endpoint") or "").strip()
    if not token_endpoint or not authorization_endpoint:
        raise XSearchError("xAI OIDC discovery response was missing required endpoints")
    if not _xai_endpoint_ok(token_endpoint) or not _xai_endpoint_ok(authorization_endpoint):
        raise XSearchError("xAI OIDC discovery returned a non-xAI endpoint")
    return {
        "token_endpoint": token_endpoint,
        "authorization_endpoint": authorization_endpoint,
    }


def _refresh_oauth_token(
    refresh_token: str,
    token_endpoint: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    if not refresh_token:
        raise XSearchError("xAI OAuth state is missing refresh_token")
    if not token_endpoint:
        token_endpoint = _oauth_discovery(timeout_seconds)["token_endpoint"]
    if not _xai_endpoint_ok(token_endpoint):
        raise XSearchError("Refusing to send xAI OAuth refresh_token to a non-xAI endpoint")

    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": XAI_OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        token_endpoint,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _user_agent(),
        },
        method="POST",
    )
    try:
        with _urlopen(request, max(5, timeout_seconds)) as response:
            payload = _read_json_response(response)
    except urllib.error.HTTPError as exc:
        detail = _redact(_read_limited(exc, MAX_ERROR_BYTES).decode("utf-8", errors="replace").strip())
        if exc.code == 403:
            raise XSearchError(
                "xAI token refresh failed with HTTP 403. This account may not "
                "be authorized for xAI API access; set XAI_API_KEY as a fallback."
            )
        raise XSearchError(
            f"xAI token refresh failed with HTTP {exc.code}"
            + (f": {detail[:500]}" if detail else "")
        )

    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise XSearchError("xAI token refresh response was missing access_token")
    return payload


def _oauth_pkce_code_verifier(length: int = 64) -> str:
    raw = base64.urlsafe_b64encode(os.urandom(length)).decode("ascii")
    return raw.rstrip("=")[:128]


def _oauth_pkce_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _oauth_build_authorize_url(
    *,
    authorization_endpoint: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    nonce: str,
) -> str:
    authorize_params = {
        "response_type": "code",
        "client_id": XAI_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": XAI_OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
        "referrer": "codex-x-search",
    }
    return f"{authorization_endpoint}?{urllib.parse.urlencode(authorize_params)}"


def _callback_cors_origin(origin: Optional[str]) -> str:
    if origin == "https://accounts.x.ai":
        return "https://accounts.x.ai"
    if origin == "https://auth.x.ai":
        return "https://auth.x.ai"
    return ""


def _make_callback_handler(
    expected_path: str,
    start_path: str,
) -> Tuple[type[BaseHTTPRequestHandler], Dict[str, Any]]:
    result: Dict[str, Any] = {
        "authorize_url": None,
        "code": None,
        "state": None,
        "error": None,
        "error_description": None,
    }
    result_lock = threading.Lock()

    class _XSearchCallbackHandler(BaseHTTPRequestHandler):
        def _maybe_write_cors_headers(self) -> None:
            origin = self.headers.get("Origin")
            allow_origin = _callback_cors_origin(origin)
            if allow_origin:
                self.send_header("Access-Control-Allow-Origin", allow_origin)
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._maybe_write_cors_headers()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == start_path:
                with result_lock:
                    authorize_url = result.get("authorize_url")
                if not authorize_url:
                    self.send_response(503)
                    self._maybe_write_cors_headers()
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body><h1>X Search sign-in is not ready.</h1></body></html>"
                    )
                    return

                escaped_url = html.escape(str(authorize_url), quote=True)
                body = (
                    "<html><head>"
                    f'<meta http-equiv="refresh" content="0; url={escaped_url}">'
                    "</head><body>"
                    "<h1>Continue to xAI sign-in.</h1>"
                    f'<p><a href="{escaped_url}">Open xAI authorization</a></p>'
                    "</body></html>"
                )
                self.send_response(200)
                self._maybe_write_cors_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return

            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found.")
                return

            params = urllib.parse.parse_qs(parsed.query)
            incoming = {
                "code": params.get("code", [None])[0],
                "state": params.get("state", [None])[0],
                "error": params.get("error", [None])[0],
                "error_description": params.get("error_description", [None])[0],
            }

            if incoming["code"] is None and incoming["error"] is None:
                self.send_response(400)
                self._maybe_write_cors_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                body = (
                    "<html><body>"
                    "<h1>X Search sign-in was not received.</h1>"
                    "<p>No authorization code was present in this callback URL. "
                    "Return to Codex and run X Search sign-in again.</p>"
                    "</body></html>"
                )
                self.wfile.write(body.encode("utf-8"))
                return

            with result_lock:
                if not (result["code"] or result["error"]):
                    result.update(incoming)

            self.send_response(200)
            self._maybe_write_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if incoming["error"]:
                body = "<html><body><h1>xAI authorization failed.</h1>You can close this tab.</body></html>"
            else:
                body = "<html><body><h1>xAI authorization received.</h1>You can close this tab.</body></html>"
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    return _XSearchCallbackHandler, result


def _start_callback_server(
    preferred_port: int = XAI_OAUTH_REDIRECT_PORT,
) -> Tuple[ThreadingHTTPServer, threading.Thread, Dict[str, Any], str]:
    handler_cls, result = _make_callback_handler(XAI_OAUTH_REDIRECT_PATH, XAI_OAUTH_START_PATH)

    class _ReuseHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    ports_to_try = [preferred_port]
    if preferred_port != 0:
        ports_to_try.append(0)
    server: Optional[ThreadingHTTPServer] = None
    last_error: Optional[OSError] = None
    for port in ports_to_try:
        try:
            server = _ReuseHTTPServer((XAI_OAUTH_REDIRECT_HOST, port), handler_cls)
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        raise XSearchError(
            f"Could not bind xAI callback server on {XAI_OAUTH_REDIRECT_HOST}:"
            f"{preferred_port}: {last_error}"
        )

    actual_port = int(server.server_address[1])
    redirect_uri = f"http://{XAI_OAUTH_REDIRECT_HOST}:{actual_port}{XAI_OAUTH_REDIRECT_PATH}"
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.1},
        daemon=True,
    )
    thread.start()
    return server, thread, result, redirect_uri


def _wait_for_callback(
    server: ThreadingHTTPServer,
    thread: threading.Thread,
    result: Dict[str, Any],
    *,
    timeout_seconds: float,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(30.0, timeout_seconds)
    try:
        while time.monotonic() < deadline:
            if result["code"] or result["error"]:
                return result
            time.sleep(0.1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)
    raise XSearchError("xAI authorization timed out waiting for the local callback")


def _exchange_code_for_tokens(
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    code_challenge: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    if not code_verifier:
        raise XSearchError(
            "xAI token exchange refused locally: PKCE code_verifier is empty. "
            "This is a bug in the X Search plugin."
        )
    if not _xai_endpoint_ok(token_endpoint):
        raise XSearchError("Refusing to send xAI authorization code to a non-xAI endpoint")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": XAI_OAUTH_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    if code_challenge:
        data["code_challenge"] = code_challenge
        data["code_challenge_method"] = "S256"

    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        token_endpoint,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _user_agent(),
        },
        method="POST",
    )
    try:
        with _urlopen(request, max(20, timeout_seconds)) as response:
            payload = _read_json_response(response)
    except urllib.error.HTTPError as exc:
        detail = _redact(_read_limited(exc, MAX_ERROR_BYTES).decode("utf-8", errors="replace").strip())
        if exc.code == 403:
            raise XSearchError(
                "xAI token exchange failed with HTTP 403. This account may not "
                "be authorized for xAI API access; set XAI_API_KEY as a fallback."
            )
        raise XSearchError(
            f"xAI token exchange failed with HTTP {exc.code}"
            + (f": {detail[:500]}" if detail else "")
        )

    if not str(payload.get("access_token") or "").strip():
        raise XSearchError("xAI token exchange response was missing access_token")
    if not str(payload.get("refresh_token") or "").strip():
        raise XSearchError("xAI token exchange response was missing refresh_token")
    return payload


def _store_oauth_payload(
    *,
    payload: Dict[str, Any],
    discovery: Dict[str, str],
    redirect_uri: str,
    base_url: str,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    auth_store = {
        "version": 1,
        "provider": "xai",
        "auth_mode": "oauth_pkce",
        "tokens": {
            "access_token": str(payload.get("access_token") or "").strip(),
            "refresh_token": str(payload.get("refresh_token") or "").strip(),
            "id_token": str(payload.get("id_token") or "").strip(),
            "expires_in": payload.get("expires_in"),
            "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
        },
        "discovery": discovery,
        "redirect_uri": redirect_uri,
        "base_url": base_url,
        "last_refresh": now,
    }
    with _auth_store_lock():
        auth_path, _ = _load_auth_store()
        _save_auth_store(auth_path, auth_store)
    return auth_store


def _run_xai_oauth_login(
    *,
    timeout_seconds: int,
    open_browser: bool,
) -> Dict[str, Any]:
    discovery = _oauth_discovery(timeout_seconds)
    server, thread, callback_result, redirect_uri = _start_callback_server()
    browser_url = f"http://127.0.0.1:{int(server.server_address[1])}/start"
    try:
        code_verifier = _oauth_pkce_code_verifier()
        code_challenge = _oauth_pkce_code_challenge(code_verifier)
        state = uuid.uuid4().hex
        nonce = uuid.uuid4().hex
        authorize_url = _oauth_build_authorize_url(
            authorization_endpoint=discovery["authorization_endpoint"],
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state,
            nonce=nonce,
        )
        callback_result["authorize_url"] = authorize_url

        print("Open this local URL to authorize X Search with xAI:", file=sys.stderr)
        print(browser_url, file=sys.stderr)
        print("Waiting for the local xAI callback.", file=sys.stderr)

        browser_opened = False
        if open_browser:
            try:
                browser_opened = bool(webbrowser.open(browser_url))
            except Exception:
                browser_opened = False
            if browser_opened:
                print("Browser opened for xAI authorization.", file=sys.stderr)
            else:
                print("Could not open a browser automatically; use the local URL above.", file=sys.stderr)

        callback = _wait_for_callback(
            server,
            thread,
            callback_result,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        try:
            thread.join(timeout=1.0)
        except Exception:
            pass
        raise

    if callback.get("error"):
        detail = callback.get("error_description") or callback["error"]
        raise XSearchError(f"xAI authorization failed: {detail}")
    if callback.get("state") != state:
        raise XSearchError("xAI authorization failed: state mismatch")
    code = str(callback.get("code") or "").strip()
    if not code:
        raise XSearchError("xAI authorization failed: missing authorization code")

    payload = _exchange_code_for_tokens(
        token_endpoint=discovery["token_endpoint"],
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
        code_challenge=code_challenge,
        timeout_seconds=timeout_seconds,
    )
    base_url = _validate_base_url(
        str(_env_value("XAI_BASE_URL") or DEFAULT_XAI_OAUTH_BASE_URL),
        "xai-oauth",
    )
    return _store_oauth_payload(
        payload=payload,
        discovery=discovery,
        redirect_uri=redirect_uri,
        base_url=base_url,
    )


def _oauth_base_url(stored_base_url: str = "") -> str:
    override = (
        _env_value("XAI_BASE_URL")
        or stored_base_url
        or DEFAULT_XAI_OAUTH_BASE_URL
    ).rstrip("/")
    return _validate_base_url(override, "xai-oauth")


def _resolve_oauth_credentials(
    timeout_seconds: int,
    *,
    force_refresh: bool = False,
) -> Optional[Tuple[str, str, str]]:
    with _auth_store_lock():
        auth_path, auth_store = _load_auth_store()
        tokens = auth_store.get("tokens") if isinstance(auth_store, dict) else None
        if not isinstance(tokens, dict):
            return None

        access_token = str(tokens.get("access_token") or "").strip()
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        should_refresh = force_refresh or _jwt_is_expiring(
            access_token,
            XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
        )
        if access_token and not should_refresh:
            return access_token, _oauth_base_url(str(auth_store.get("base_url") or "")), "xai-oauth"

        if not refresh_token:
            return None

        discovery = auth_store.get("discovery") if isinstance(auth_store.get("discovery"), dict) else {}
        updated_discovery = dict(discovery)
        token_endpoint = str(updated_discovery.get("token_endpoint") or "").strip()
        if not token_endpoint:
            token_endpoint = _oauth_discovery(timeout_seconds)["token_endpoint"]
            updated_discovery["token_endpoint"] = token_endpoint
        refreshed = _refresh_oauth_token(refresh_token, token_endpoint, timeout_seconds)

        updated_tokens = dict(tokens)
        updated_tokens["access_token"] = str(refreshed.get("access_token") or "").strip()
        updated_tokens["refresh_token"] = str(refreshed.get("refresh_token") or refresh_token).strip()
        if refreshed.get("id_token"):
            updated_tokens["id_token"] = str(refreshed.get("id_token") or "").strip()
        if refreshed.get("expires_in") is not None:
            updated_tokens["expires_in"] = refreshed.get("expires_in")
        if refreshed.get("token_type"):
            updated_tokens["token_type"] = str(refreshed.get("token_type") or "Bearer").strip() or "Bearer"

        auth_store["tokens"] = updated_tokens
        auth_store["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        auth_store["auth_mode"] = "oauth_pkce"
        auth_store["discovery"] = updated_discovery
        _save_auth_store(auth_path, auth_store)
        return updated_tokens["access_token"], _oauth_base_url(str(auth_store.get("base_url") or "")), "xai-oauth"


def _resolve_xai_credentials(
    timeout_seconds: int,
    *,
    force_refresh: bool = False,
) -> Tuple[str, str, str]:
    api_key = str(_env_value("XAI_API_KEY") or "").strip()
    if api_key and _x_search_config()["credential_preference"] == "api_key":
        base_url = _validate_base_url(str(_env_value("XAI_BASE_URL") or DEFAULT_XAI_BASE_URL), "xai")
        return api_key, base_url, "xai"
    oauth_error: Optional[XSearchError] = None
    try:
        oauth = _resolve_oauth_credentials(timeout_seconds, force_refresh=force_refresh)
        if oauth:
            return oauth
    except XSearchError as exc:
        oauth_error = exc

    if api_key:
        base_url = _validate_base_url(str(_env_value("XAI_BASE_URL") or DEFAULT_XAI_BASE_URL), "xai")
        return api_key, base_url, "xai"

    if oauth_error:
        raise XSearchNoCredentialsError(
            "X Search authentication needs to be refreshed. Ask the user to "
            f"run x_search_auth, then retry x_search. Details: {oauth_error}"
        ) from oauth_error
    raise XSearchNoCredentialsError(
        "X Search authentication is required. Ask the user to run "
        "x_search_auth, then retry x_search."
    )


def _has_configured_credentials() -> bool:
    try:
        timeout_seconds = int(_x_search_config()["timeout_seconds"])
        token, _, _ = _resolve_xai_credentials(timeout_seconds)
        return bool(str(token or "").strip())
    except Exception:
        return False


def check_x_search_requirements() -> bool:
    return _has_configured_credentials()


def _normalize_handles(value: Any, field_name: str) -> List[str]:
    if value is None:
        items: Iterable[Any] = []
    elif isinstance(value, list):
        items = value
    else:
        raise XSearchError(f"{field_name} must be an array of strings")

    cleaned: List[str] = []
    for handle in items:
        if not isinstance(handle, str):
            raise XSearchError(f"{field_name} must contain only strings")
        if not handle.strip():
            continue
        try:
            normalized = normalize_handle(handle)
        except ValueError as exc:
            raise XSearchError(f"{field_name}: {exc}") from exc
        if normalized.lower() not in {h.lower() for h in cleaned}:
            cleaned.append(normalized)
    if len(cleaned) > MAX_HANDLES:
        raise XSearchError(f"{field_name} supports at most {MAX_HANDLES} handles")
    return cleaned


def _bool_arg(arguments: Dict[str, Any], field_name: str, default: bool = False) -> bool:
    value = arguments.get(field_name, default)
    if isinstance(value, bool):
        return value
    raise XSearchError(f"{field_name} must be a boolean")


def _int_arg(
    arguments: Dict[str, Any],
    field_name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = arguments.get(field_name, default)
    if isinstance(value, bool):
        raise XSearchError(f"{field_name} must be an integer")
    try:
        parsed = int(value)
    except Exception as exc:
        raise XSearchError(f"{field_name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise XSearchError(f"{field_name} must be between {minimum} and {maximum}")
    return parsed


def _parse_iso_date(value: str, field_name: str) -> date:
    raw = value.strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
        raise XSearchError(f"{field_name} must be YYYY-MM-DD (got {raw!r})")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise XSearchError(f"{field_name} must be YYYY-MM-DD (got {raw!r})") from exc


def _validate_date_range(from_date: str, to_date: str) -> None:
    parsed_from: Optional[date] = None
    parsed_to: Optional[date] = None
    if from_date.strip():
        parsed_from = _parse_iso_date(from_date, "from_date")
    if to_date.strip():
        parsed_to = _parse_iso_date(to_date, "to_date")
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise XSearchError(
            f"from_date ({parsed_from.isoformat()}) must be on or before "
            f"to_date ({parsed_to.isoformat()})"
        )
    if parsed_from is not None:
        today_utc = datetime.now(timezone.utc).date()
        if parsed_from > today_utc:
            raise XSearchError(
                f"from_date ({parsed_from.isoformat()}) is in the future; "
                f"X Search only indexes past posts (today UTC is {today_utc.isoformat()})"
            )


def _extract_response_text(payload: Dict[str, Any]) -> str:
    output_text = str(payload.get("output_text") or "").strip()
    if output_text:
        return output_text

    parts: List[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"}:
                text = str(content.get("text") or "").strip()
                if text:
                    parts.append(text)
    return "\n\n".join(parts).strip()


def _extract_inline_citations(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    citations: List[Dict[str, Any]] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            for annotation in content.get("annotations", []) or []:
                if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                    continue
                citations.append(
                    {
                        "url": annotation.get("url", ""),
                        "title": annotation.get("title", ""),
                        "start_index": annotation.get("start_index"),
                        "end_index": annotation.get("end_index"),
                    }
                )
    return citations


def _looks_like_latest_post_query(query: str) -> bool:
    normalized = query.lower()
    topic_markers = (
        " about ",
        " regarding ",
        " around ",
        " on ",
        " عن ",
        " حول ",
        " بخصوص ",
    )
    if any(marker in normalized for marker in topic_markers):
        return False
    latest_terms = (
        "latest",
        "newest",
        "recent",
        "last post",
        "last tweet",
        "آخر",
        "اخر",
        "أحدث",
        "احدث",
    )
    post_terms = (
        "post",
        "tweet",
        "status",
        "تغريدة",
        "تغريده",
        "منشور",
        "بوست",
    )
    author_markers = (
        " by ",
        " from ",
        " account",
        " handle",
        " profile",
        "نزلها",
        "نزلته",
        "كتبها",
        "حساب",
        "من ",
    )
    return any(term in normalized for term in latest_terms) and any(
        term in normalized for term in post_terms
    ) and any(marker in normalized for marker in author_markers)


def _effective_query(query: str, allowed: List[str], excluded: List[str]) -> Tuple[str, Optional[str]]:
    if allowed or excluded or not _looks_like_latest_post_query(query):
        return query, None
    strategy = "latest_author_post"
    return (
        query
        + "\n\n"
        + "X Search instruction: If this asks for the latest X post by a named "
        "person or organization and no handle filter is provided, first identify "
        "the most relevant official or verified X account for that name. Then "
        "return the newest original post from that account with its X URL. Do "
        "not return replies unless no original post is available, and say when "
        "the result is a reply rather than an original post.",
        strategy,
    )


def _user_agent() -> str:
    return f"Codex-X-Search/{SERVER_VERSION}"


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        body = _redact(_read_limited(exc, MAX_ERROR_BYTES).decode("utf-8", errors="replace").strip())
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            code = str(payload.get("code") or "").strip()
            error = str(payload.get("error") or payload.get("message") or "").strip()
            message = error or str(payload)
            if code and code not in message:
                message = f"{code}: {message}"
            return message
        return body[:500] or str(exc)
    finally:
        with contextlib.suppress(Exception):
            exc.close()


def _post_json(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_seconds: int,
) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    with _urlopen(request, timeout_seconds) as response:
        return _read_json_response(response)


def _retry_after_seconds(headers: Any) -> Optional[float]:
    raw = str((headers or {}).get("Retry-After", "")).strip()
    if not raw:
        return None
    try:
        if re.fullmatch(r"[0-9]+", raw):
            return float(raw)
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, OverflowError, TypeError):
        return None


def _search_request(payload, api_key, base_url, source, config, timeout_seconds):
    """Bound retries by elapsed time; auth refresh has its own one-attempt allowance."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": _user_agent()}
    started = time.monotonic()
    deadline = started + timeout_seconds
    retries = attempts = 0
    refreshed = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = _tool_error("x_search", "X search request budget exhausted", "TimeoutError")
            break
        attempts += 1
        delay = min(5.0, 1.5 * (retries + 1))
        try:
            data = _post_json(f"{base_url}/responses", headers, payload, min(timeout_seconds, remaining))
            return data, source, {"attempts": attempts, "elapsed_seconds": round(time.monotonic() - started, 3)}, None
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry_after = _retry_after_seconds(exc.headers)
            message = _http_error_message(exc)
            if status == 401 and source == "xai-oauth" and not refreshed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = _tool_error("x_search", "X search request budget exhausted", "TimeoutError")
                    break
                api_key, base_url, source = _resolve_xai_credentials(remaining, force_refresh=True)
                headers["Authorization"] = f"Bearer {api_key}"
                refreshed = True
                continue
            error = _tool_error("x_search", message, "HTTPError")
            error["http_status"] = status
            error["retryable"] = status == 429 or 500 <= status < 600
            if retry_after is not None:
                error["retry_after_seconds"] = retry_after
                delay = max(delay, retry_after)
            if not error["retryable"]:
                break
        except (TimeoutError, OSError) as exc:
            error = _tool_error("x_search", _redact(str(exc)), type(exc).__name__)
            error["retryable"] = True
        if retries >= config["retries"] or delay >= deadline - time.monotonic():
            break
        retries += 1
        time.sleep(delay)
    diagnostics = {"attempts": attempts, "elapsed_seconds": round(time.monotonic() - started, 3)}
    error["diagnostics"] = diagnostics
    return None, source, diagnostics, error


def x_search_auth_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    allowed_keys = set(X_SEARCH_AUTH_INPUT_SCHEMA["properties"])
    unknown_keys = sorted(set(arguments) - allowed_keys)
    if unknown_keys:
        raise XSearchError(f"unknown x_search_auth argument(s): {', '.join(unknown_keys)}")

    allow_redirect = _bool_arg(arguments, "allow_redirect", False)
    force = _bool_arg(arguments, "force", False)
    open_browser = _bool_arg(arguments, "open_browser", True)
    timeout_seconds = _int_arg(
        arguments,
        "timeout_seconds",
        default=180,
        minimum=30,
        maximum=900,
    )

    if not force:
        try:
            token, base_url, source = _resolve_xai_credentials(timeout_seconds)
            if token:
                return {
                    "success": True,
                    "provider": "xai",
                    "tool": "x_search_auth",
                    "authenticated": True,
                    "credential_source": source,
                    "base_url": base_url,
                    "message": "X Search is already authenticated.",
                }
        except XSearchNoCredentialsError:
            pass

    if not allow_redirect:
        return {
            "success": True,
            "provider": "xai",
            "tool": "x_search_auth",
            "authenticated": False,
            "permission_required": True,
            "permission_prompt": X_SEARCH_AUTH_PERMISSION_MESSAGE,
            "allow_tool": "x_search_auth",
            "allow_arguments": {
                "allow_redirect": True,
                "open_browser": open_browser,
                "timeout_seconds": timeout_seconds,
                "force": force,
            },
            "message": X_SEARCH_AUTH_PERMISSION_MESSAGE,
        }

    auth_store = _run_xai_oauth_login(
        timeout_seconds=timeout_seconds,
        open_browser=open_browser,
    )
    return {
        "success": True,
        "provider": "xai",
        "tool": "x_search_auth",
        "authenticated": True,
        "credential_source": "xai-oauth",
        "base_url": auth_store.get("base_url") or DEFAULT_XAI_OAUTH_BASE_URL,
        "message": "xAI sign-in completed for X Search.",
    }


def x_search_status_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    if arguments:
        raise XSearchError("x_search_status does not accept arguments")
    auth_path, auth_store = _load_auth_store()
    tokens = auth_store.get("tokens") if isinstance(auth_store, dict) else None
    has_oauth = bool(
        isinstance(tokens, dict)
        and str(tokens.get("access_token") or "").strip()
    )
    has_api_key = bool(str(_env_value("XAI_API_KEY") or "").strip())
    authenticated = False
    credential_source: Optional[str] = None
    base_url: Optional[str] = None
    error: Optional[str] = None
    config = None
    try:
        config = _x_search_config()
        timeout_seconds = int(config["timeout_seconds"])
        token, resolved_base_url, source = _resolve_xai_credentials(timeout_seconds)
        authenticated = bool(str(token or "").strip())
        credential_source = source if authenticated else None
        base_url = resolved_base_url if authenticated else None
    except XSearchNoCredentialsError as exc:
        error = str(exc)
    except XSearchError as exc:
        error = str(exc)
    return {
        "success": True,
        "provider": "xai",
        "tool": "x_search_status",
        "authenticated": authenticated,
        "oauth_configured": has_oauth,
        "api_key_configured": has_api_key,
        "credential_source": credential_source,
        "base_url": base_url,
        "auth_store": str(auth_path),
        "error": error,
        "server_version": SERVER_VERSION,
        "config": config,
        "capabilities": ["search", "account_research", "thread_research", "citation_metadata"],
    }


def x_search_logout_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    if arguments:
        raise XSearchError("x_search_logout does not accept arguments")
    removed = _delete_auth_store()
    return {
        "success": True,
        "provider": "xai",
        "tool": "x_search_logout",
        "removed": removed,
        "message": "Removed stored xAI OAuth credentials for X Search." if removed else "No stored xAI OAuth credentials were present.",
    }


def x_search_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    allowed_keys = set(X_SEARCH_INPUT_SCHEMA["properties"])
    unknown_keys = sorted(set(arguments) - allowed_keys)
    if unknown_keys:
        raise XSearchError(f"unknown x_search argument(s): {', '.join(unknown_keys)}")

    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise XSearchError("query is required for x_search")
    query = query.strip()
    if len(query) > 20000:
        raise XSearchError("query must be at most 20000 characters")

    config = _x_search_config()
    timeout_seconds = config["timeout_seconds"]
    if "timeout_seconds" in arguments:
        if isinstance(arguments["timeout_seconds"], bool) or not isinstance(arguments["timeout_seconds"], int):
            raise XSearchError("timeout_seconds must be an integer")
        timeout_seconds = _int_arg(arguments, "timeout_seconds", default=timeout_seconds, minimum=10, maximum=300)
    require_citations = _bool_arg(arguments, "require_citations")
    reasoning_effort = arguments.get("reasoning_effort", config["reasoning_effort"])
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        raise XSearchError("reasoning_effort must be one of: " + ", ".join(REASONING_EFFORTS))

    allowed = _normalize_handles(arguments.get("allowed_x_handles"), "allowed_x_handles")
    excluded = _normalize_handles(arguments.get("excluded_x_handles"), "excluded_x_handles")
    if allowed and excluded:
        raise XSearchError("allowed_x_handles and excluded_x_handles cannot be used together")

    for field in ("from_date", "to_date"):
        if field in arguments and not isinstance(arguments[field], str):
            raise XSearchError(f"{field} must be a string")
    from_date = arguments.get("from_date", "").strip()
    to_date = arguments.get("to_date", "").strip()
    _validate_date_range(from_date, to_date)
    enable_image_understanding = _bool_arg(arguments, "enable_image_understanding")
    enable_video_understanding = _bool_arg(arguments, "enable_video_understanding")

    api_key, base_url, source = _resolve_xai_credentials(timeout_seconds)

    tool_def: Dict[str, Any] = {"type": "x_search"}
    if allowed:
        tool_def["allowed_x_handles"] = allowed
    if excluded:
        tool_def["excluded_x_handles"] = excluded
    if from_date:
        tool_def["from_date"] = from_date
    if to_date:
        tool_def["to_date"] = to_date
    if enable_image_understanding:
        tool_def["enable_image_understanding"] = True
    if enable_video_understanding:
        tool_def["enable_video_understanding"] = True

    effective_query, query_strategy = _effective_query(query, allowed, excluded)
    payload = {
        "model": config["model"],
        "input": [{"role": "user", "content": effective_query}],
        "tools": [tool_def],
        "store": False,
    }
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    response_payload, source, diagnostics, error = _search_request(
        payload, api_key, base_url, source, config, timeout_seconds,
    )
    if error:
        return error

    answer = _extract_response_text(response_payload)
    raw_citations = response_payload.get("citations")
    citations = raw_citations if isinstance(raw_citations, list) else []
    inline_citations = _extract_inline_citations(response_payload)
    sources = collect_sources(citations, inline_citations)

    active_filters: List[str] = []
    if allowed:
        active_filters.append("allowed_x_handles")
    if excluded:
        active_filters.append("excluded_x_handles")
    if from_date:
        active_filters.append("from_date")
    if to_date:
        active_filters.append("to_date")
    response_status = response_payload.get("status", "completed")
    degraded = not sources or response_status != "completed" or not answer
    if not sources:
        reason = (f"no citations returned despite filters: {', '.join(active_filters)}" if active_filters and not citations and not inline_citations
                  else "no usable X citations returned")
    elif not answer:
        reason = "provider returned no answer text"
    elif response_status != "completed":
        reason = f"provider response status: {response_status}"
    else:
        reason = None

    result = {
        "success": not (require_citations and degraded),
        "provider": "xai",
        "credential_source": source,
        "tool": "x_search",
        "model": payload["model"],
        "query": query,
        "query_strategy": query_strategy,
        "answer": answer,
        "citations": citations,
        "inline_citations": inline_citations,
        "sources": sources,
        "source_count": len(sources),
        "evidence": "x_posts_cited" if any(s["kind"] == "post" for s in sources) else "x_profiles_cited" if sources else "uncited",
        "coverage": "search_sample",
        "warnings": source_warnings(sources, allowed, excluded, from_date, to_date),
        "diagnostics": diagnostics,
        "response_status": response_status,
        "usage": response_payload.get("usage") if isinstance(response_payload.get("usage"), dict) else None,
        "degraded": degraded,
        "degraded_reason": reason,
    }
    if not result["success"]:
        result.update(error=reason, error_type="InsufficientEvidence")
    return result


def x_search_research_tool(kind: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    schema = research_schema(kind, X_SEARCH_INPUT_SCHEMA["properties"])
    unknown = set(arguments) - set(schema["properties"])
    if unknown:
        raise XSearchError("unknown research argument(s): " + ", ".join(sorted(unknown)))
    try:
        search_args, target = research_arguments(kind, arguments)
    except ValueError as exc:
        raise XSearchError(str(exc)) from exc
    result = x_search_tool(search_args)
    result["tool"] = f"x_search_{kind}"
    result["target"] = target
    if "sources" in result:
        if kind == "thread":
            result["target_cited"] = any(s.get("post_id") == target["post_id"] for s in result["sources"])
            missing_reason = "requested root post was not cited; thread identity is unverified"
        else:
            result["target_cited"] = any(
                s["kind"] == "post" and (s.get("handle") or "").lower() == target["handle"].lower()
                for s in result["sources"]
            )
            missing_reason = "no post from the requested account was cited"
        if not result["target_cited"]:
            result["degraded"] = True
            result["degraded_reason"] = missing_reason
            if search_args["require_citations"]:
                result.update(success=False, error=result["degraded_reason"], error_type="InsufficientEvidence")
    return result


def _tool_error(tool_name: str, message: str, error_type: str = "XSearchError") -> Dict[str, Any]:
    return {
        "success": False,
        "provider": "xai",
        "tool": tool_name,
        "error": message,
        "error_type": error_type,
    }


def _auth_required_error(tool_name: str, message: str) -> Dict[str, Any]:
    result = _tool_error(tool_name, message, "XSearchNoCredentialsError")
    result["auth_required"] = True
    result["auth_tool"] = "x_search_auth"
    result["permission_required"] = True
    result["permission_prompt"] = X_SEARCH_AUTH_PERMISSION_MESSAGE
    result["allow_tool"] = "x_search_auth"
    result["allow_arguments"] = {"allow_redirect": True}
    return result


def _read_mcp_message() -> Optional[Tuple[Dict[str, Any], str]]:
    first_line = sys.stdin.buffer.readline()
    if first_line == b"":
        return None

    stripped_first = first_line.strip()
    if stripped_first.startswith(b"{"):
        try:
            payload = json.loads(stripped_first.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise McpProtocolError(-32700, f"Parse error: {exc}") from exc
        if not isinstance(payload, dict):
            raise McpProtocolError(-32600, "Invalid Request")
        return payload, "newline"

    headers: Dict[str, str] = {}
    decoded_first = first_line.decode("ascii", errors="replace").strip()
    if ":" in decoded_first:
        key, value = decoded_first.split(":", 1)
        headers[key.lower()] = value.strip()
    elif decoded_first:
        raise McpProtocolError(-32600, "Invalid Request")
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        decoded = line.decode("ascii", errors="replace").strip()
        if decoded == "":
            break
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.lower()] = value.strip()

    length_raw = headers.get("content-length")
    if not length_raw:
        raise McpProtocolError(-32600, "Missing Content-Length")
    try:
        content_length = int(length_raw)
    except ValueError as exc:
        raise McpProtocolError(-32600, "Invalid Content-Length") from exc
    if content_length < 0 or content_length > MAX_MCP_CONTENT_LENGTH:
        raise McpProtocolError(-32600, "Invalid Content-Length")
    body = sys.stdin.buffer.read(content_length)
    if len(body) != content_length:
        raise McpProtocolError(-32700, "Unexpected EOF while reading message body")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise McpProtocolError(-32700, f"Parse error: {exc}") from exc
    if not isinstance(payload, dict):
        raise McpProtocolError(-32600, "Invalid Request")
    return payload, "content-length"


def _send_mcp_message(payload: Dict[str, Any], framing: str = "newline") -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if framing == "content-length":
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(body)
    else:
        sys.stdout.buffer.write(body + b"\n")
    sys.stdout.buffer.flush()


def _mcp_result(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def _mcp_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    handlers = {
        "x_search": x_search_tool,
        "x_search_account": lambda args: x_search_research_tool("account", args),
        "x_search_thread": lambda args: x_search_research_tool("thread", args),
        "x_search_auth": x_search_auth_tool,
        "x_search_status": x_search_status_tool,
        "x_search_logout": x_search_logout_tool,
    }
    handler = handlers.get(name)
    if handler is None:
        raise KeyError(name)
    return handler(arguments)


def _handle_mcp_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        return _mcp_error(None, -32600, "Invalid Request")
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if request_id is None:
        return None

    if method == "initialize":
        requested_version = str(params.get("protocolVersion") or SUPPORTED_PROTOCOL_VERSIONS[0])
        protocol_version = (
            requested_version
            if requested_version in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return _mcp_result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "ping":
        return _mcp_result(request_id, {})

    if method == "tools/list":
        return _mcp_result(request_id, {"tools": [X_SEARCH_TOOL, *RESEARCH_TOOLS, *SETUP_TOOLS]})

    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            result = _call_tool(name, arguments)
        except KeyError:
            return _mcp_error(request_id, -32602, f"Unknown tool: {name}")
        except XSearchNoCredentialsError as exc:
            result = _auth_required_error(name, str(exc))
        except XSearchError as exc:
            result = _tool_error(name, str(exc))
        except Exception as exc:
            result = _tool_error(name, str(exc), type(exc).__name__)
        return _mcp_result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False, indent=2),
                    }
                ],
                "isError": not bool(result.get("success")),
            },
        )

    if method in {"resources/list", "prompts/list"}:
        key = "resources" if method == "resources/list" else "prompts"
        return _mcp_result(request_id, {key: []})

    return _mcp_error(request_id, -32601, f"Method not found: {method}")


def _run_cli_auth(argv: List[str]) -> int:
    force = "--force" in argv
    no_browser = "--no-browser" in argv
    timeout_seconds = 180
    for index, item in enumerate(argv):
        if item == "--timeout" and index + 1 < len(argv):
            try:
                timeout_seconds = int(argv[index + 1])
            except ValueError:
                print("--timeout must be an integer", file=sys.stderr)
                return 2
    try:
        if force:
            _delete_auth_store()
        x_search_auth_tool(
            {
                "allow_redirect": True,
                "open_browser": not no_browser,
                "timeout_seconds": timeout_seconds,
                "force": force,
            }
        )
    except XSearchError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "success": True,
                "provider": "xai",
                "tool": "x_search_auth",
                "authenticated": True,
                "message": "X Search authentication completed.",
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"auth", "login", "auth-add"}:
        return _run_cli_auth(sys.argv[2:])

    while True:
        try:
            read_result = _read_mcp_message()
        except McpProtocolError as exc:
            _send_mcp_message(_mcp_error(None, exc.code, exc.message))
            continue
        except Exception as exc:
            _send_mcp_message(_mcp_error(None, -32603, str(exc)))
            continue
        if read_result is None:
            return 0
        message, framing = read_result
        response = _handle_mcp_request(message)
        if response is not None:
            _send_mcp_message(response, framing)


if __name__ == "__main__":
    raise SystemExit(main())
