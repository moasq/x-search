# X Search for Codex

Codex marketplace for a standalone plugin that exposes an MCP tool named
`x_search`. It calls
xAI's Responses API with the server-side `{"type": "x_search"}` tool and owns
its own xAI sign-in flow.

This is [moasq's fork](https://github.com/moasq/x-search) of
[Abdullah4AI/x-search](https://github.com/Abdullah4AI/x-search). Original authorship
and the MIT license are preserved. See [UPSTREAM.md](UPSTREAM.md) for the sync baseline.

## Add to Codex

In Codex, open **Add marketplace** and use:

- Source: `https://github.com/moasq/x-search.git`
- Git ref: `main`
- Sparse paths: leave empty

If you prefer a sparse checkout, use these two sparse paths instead:

```text
.agents/plugins/marketplace.json
plugins/x-search
```

The repository root contains `.agents/plugins/marketplace.json`, and the plugin
itself lives at `plugins/x-search`, which is the layout Codex expects for a Git
marketplace.

### Windows compatibility

X Search is implemented in Python, but Codex starts it through a small Node.js
launcher so Windows installs are not forced to have a `python3` command. The
launcher tries `py -3`, `py`, `python`, and `python3` on Windows, and
`python3` then `python` on macOS/Linux. It starts the MCP server with the first
Python 3.9+ runtime it finds. If Codex installs the plugin but
`x_search_status` and `x_search` do not appear, install Python 3.9 or newer from
https://www.python.org/downloads/ and make sure the Python launcher is on your
PATH.

## Features

- Search X posts, profiles, and threads from Codex
- `allowed_x_handles` and `excluded_x_handles` filters, max 20 handles
- `from_date` and `to_date` validation with `YYYY-MM-DD`
- `enable_image_understanding` and `enable_video_understanding`
- xAI citations and inline URL citation extraction
- Account and thread research tools with citation requirements enabled by default
- Normalized, deduplicated X citation records with post IDs and inferred dates
- `degraded` results for uncited, empty, or incomplete answers, including broad searches
- Optional reasoning effort and per-call retry budgets
- Retries for 429 and 5xx responses, respecting `Retry-After` and elapsed time
- Usage, elapsed time, attempt counts, and source/filter mismatch warnings
- Precision hint for latest-post questions without a known handle
- System-CA-backed HTTPS verification, with `X_SEARCH_CA_BUNDLE` override
- Built-in browser-based xAI OAuth PKCE sign-in with local token storage
- `XAI_API_KEY` fallback from `~/.codex-x-search/.env` or the process environment
- Local config through `~/.codex-x-search/config.json` or environment variables

## Authentication

The plugin resolves credentials in this order:

1. xAI OAuth token stored by this plugin in `~/.codex-x-search/auth.json`
2. `XAI_API_KEY` from `~/.codex-x-search/.env`
3. `XAI_API_KEY` from the current process environment

OAuth remains the default preference. To prefer an explicitly configured,
metered API key (as Hermes does), set `credential_preference` to `api_key`, or
`X_SEARCH_CREDENTIAL_PREFERENCE=api_key`. OAuth is still used if no key is
configured. `x_search_status` reports the selected credential source and effective
configuration without secret values.

Before using `x_search`, check authentication with `x_search_status`. If the
user is already authenticated, Codex can use `x_search` immediately. If the
user is not authenticated, call `x_search_auth` without `allow_redirect`; the
tool returns a permission prompt and does not open a browser. Only after the
user allows the redirect should Codex call `x_search_auth` again with
`{"allow_redirect": true}`.

The `x_search_auth` tool:

1. It first verifies whether a valid credential already exists.
2. If no credential exists and `allow_redirect` is not true, it returns:
   `X Search needs to open the xAI authentication page to complete sign-in. Do
   you want to allow this?`
3. After the user allows the redirect, it starts a temporary callback server on
   `127.0.0.1`.
4. It opens the xAI authorization page in your default browser.
5. After you approve access, it stores the token locally for your user only.

You can also sign in from a terminal:

```bash
python3 plugins/x-search/scripts/x_search_mcp_server.py auth
```

On Windows, use one of these forms if `python3` is not available:

```powershell
py -3 plugins/x-search/scripts/x_search_mcp_server.py auth
python plugins/x-search/scripts/x_search_mcp_server.py auth
node plugins/x-search/scripts/x_search_mcp_launcher.js auth
```

Tokens are stored outside the plugin repository in `~/.codex-x-search/auth.json`
with file mode `0600` where the platform allows it.

## Credential Isolation

This repository does not include xAI credentials, OAuth tokens, API keys, or
local credential files. The MCP server resolves credentials only at runtime from
the current user's machine:

- the current user's `~/.codex-x-search/auth.json`
- the current user's `~/.codex-x-search/.env`
- the current process environment

Sharing this plugin does not share your local credential directory or your
environment variables. Anyone who installs the plugin needs their own xAI
sign-in or their own `XAI_API_KEY`.

The repository `.gitignore` excludes common credential files such as `.env`,
`auth.json`, `.codex-x-search/`, and private key formats. Keep those files local
and out of commits.

## Configuration

The x_search model, timeout, and retry settings can be set in
`~/.codex-x-search/config.json`:

```json
{
  "x_search": {
    "model": "grok-4.20-reasoning",
    "timeout_seconds": 75,
    "retries": 1
  }
}
```

Environment variables override the config file:

- `X_SEARCH_MODEL`
- `X_SEARCH_TIMEOUT_SECONDS`
- `X_SEARCH_RETRIES`
- `X_SEARCH_REASONING_EFFORT` (`low`, `medium`, `high`, or `xhigh`)
- `X_SEARCH_CREDENTIAL_PREFERENCE` (`oauth` by default, or `api_key`)
- `X_SEARCH_HOME`
- `X_SEARCH_CA_BUNDLE`
- `XAI_API_KEY`
- `XAI_BASE_URL`

`reasoning_effort` is omitted by default; only set it for a model that supports
that effort. Configure `reasoning_effort` and `credential_preference` under
`x_search` in the same JSON file if preferred. Invalid values fail before search.
The model default is retained for compatibility with existing OAuth installations;
use `X_SEARCH_MODEL` to choose a different supported model.

Timeouts are a **retry budget** (10–300 seconds; default 75), not a fresh timeout
for every attempt. Each attempt receives the remaining budget as its socket
timeout. Up to three retries can be configured. Initial credential resolution is
outside this budget; OS socket/DNS behavior and OAuth refresh mean this is not a
hard real-time cancellation guarantee. A long `Retry-After` is returned to the
caller instead of sleeping past the budget. A 401 permits one OAuth refresh and
replay even when ordinary retries are disabled. Retrying a request whose response
was lost may incur an additional provider charge.

For safety, `XAI_BASE_URL` must point to an HTTPS `x.ai` host. If you
intentionally use a trusted proxy for API-key traffic, set:

```bash
X_SEARCH_ALLOW_CUSTOM_BASE_URL=1
```

HTTPS requests first try `X_SEARCH_CA_BUNDLE` when set, then common system CA
bundle locations, then `certifi`, and finally Python's default trust store. If
your Python installation still has a broken CA setup, set `X_SEARCH_CA_BUNDLE`
to a valid CA bundle file.

## Tool Parameters

`x_search` accepts:

- `query` string, required
- `allowed_x_handles` string array
- `excluded_x_handles` string array
- `from_date` string, `YYYY-MM-DD`
- `to_date` string, `YYYY-MM-DD`
- `enable_image_understanding` boolean
- `enable_video_understanding` boolean
- `reasoning_effort` enum, optional override of configuration
- `timeout_seconds` integer, optional retry-budget override
- `require_citations` boolean, default false

The result is JSON text with `success`, `answer`, `citations`,
`inline_citations`, `degraded`, `degraded_reason`, `credential_source`, `model`,
`query_strategy`, and `query`. It also includes:

| Field | Meaning |
| --- | --- |
| `sources` | Deduplicated X URLs from provider citations, with citation channels, post IDs or profile handles |
| `source_count` | Unique cited X sources, not the number of posts searched or billed |
| `evidence` | `x_posts_cited`, `x_profiles_cited`, or `uncited` |
| `coverage` | Always `search_sample`; no exhaustive coverage claim |
| `warnings` | Cited handle/date mismatches or future post IDs; quoted/context posts can explain some mismatches |
| `diagnostics` | Search HTTP attempts and elapsed seconds |
| `usage` | Provider usage object when supplied; otherwise null |
| `response_status` | Provider completion status |

Source timestamps are derived from the X post ID, not a fresh read of that post.
`content_verified: false` makes this distinction explicit. These records contain
citation metadata, not raw post bodies or verified engagement counts. URLs that
appear only in generated answer text are not promoted to citation evidence.
`require_citations: true` returns `success: false` / MCP `isError: true` for
uncited, incomplete, or empty answers while preserving the answer for inspection.

### Account research

```json
{"handle":"NASA","max_posts":5,"include_replies":false,"focus":"Recent space missions"}
```

Pass this to `x_search_account`. Optional dates, media-understanding flags,
reasoning effort, and timeout work as in `x_search`. It requests recent posts
with dates and links, separates paraphrases from quotes, and asks for retrieval
gaps. This is public discovery, not access to your authenticated X account.
The default evidence check requires a cited post from the requested handle;
a profile link or another account's post alone does not pass.

### Thread research

```json
{"url":"https://x.com/NASA/status/2102147059411263495","max_posts":10,"focus":"Main questions in the replies"}
```

Pass this to `x_search_thread`. It asks for the root post, author continuations,
and other users' replies separately. The root post must be cited for the default
strict check to pass; `target_cited` reports that check. Canonical X and Twitter
post URLs are accepted. No arbitrary webpage fetching is added.

Both research tools default to `require_citations: true`. `max_posts` (1–50) is
an instruction about the answer length, **not** a provider retrieval, pagination,
or billing limit. The underlying Grok tool controls retrieval. These tools do
not provide Felo-style raw timeline exports or cursor pagination; those require
a separate data API. None of the tools posts, replies, likes, or sends DMs.

Additional MCP tools:

- `x_search_auth`: checks existing access, asks permission before opening the
  xAI sign-in page, and stores a local token after authorization
- `x_search_status`: reports whether this user has a local credential
- `x_search_logout`: removes the stored OAuth token for this user

The research tools share these credentials and do not require Hermes, Felo,
an X developer account, or additional Python dependencies.

`x_search` is always exposed so Codex can find the right tool immediately. If
credentials are missing, calling it returns an auth-required response that
points Codex to `x_search_auth` without starting browser sign-in.

## Validation

Run the local test suite with:

```bash
python3 -m unittest discover -s tests -v
```

On Windows, use `py -3 -m unittest discover -s tests -v` or
`python -m unittest discover -s tests -v` if `python3` is not on PATH.

The tests cover the xAI Responses payload shape, filter/date validation,
degraded-result signaling, latest-post query hinting, cert-aware HTTPS calls,
standalone OAuth refresh persistence, browser sign-in tool wiring, and MCP
`tools/list` / `tools/call` behavior, plus the cross-platform launcher contract.

CI also checks account/thread evidence, 429 handling, retry budgets, strict
validation, credential preference, and the stdio server on Linux, macOS, and
Windows. Tests use synthetic credentials and mocked provider calls; live checks
are separate and are not run automatically by CI.
