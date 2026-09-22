# Upstream tracking

Checked 2026-09-22.

| Item | Local baseline | Upstream | Action |
| --- | --- | --- | --- |
| GitHub fork | `moasq/x-search:main` at `b140aef9bd015dd0a955111842ed1dd0f3f2c68c` | `Abdullah4AI/x-search:main` at the same commit | Already synchronized; retain history |
| Installed marketplace checkout observed during development | `ca757bd77a5fd168a6fd7847a0e498dff08e1bd5` | `b140aef9bd015dd0a955111842ed1dd0f3f2c68c` | Build this release from current upstream, including its Windows launcher |
| Hermes X Search | Current official `tools/x_search_tool.py` inspected | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/blob/main/tools/x_search_tool.py) | Adapt configurable reasoning and explicit API-key preference; no Hermes runtime dependency |
| xAI handle filters | Previous limit 10 | [xAI X Search docs](https://docs.x.ai/developers/tools/x-search) document 20 | Raise limit; validate and deduplicate handles |

## 0.3.0 changes

- Account and thread research tools, with explicit sample coverage and strict citation requirements.
- Citation metadata, source deduplication, post-ID dates, and filter/date warnings.
- Broad uncited answers now report degraded evidence.
- Reasoning configuration, request diagnostics, and provider usage passthrough.
- 429/5xx retries constrained by elapsed budget and `Retry-After`.
- OAuth 401 refresh/replay independent of the ordinary retry count.
- Opt-in API-key preference; existing OAuth-first behavior preserved.
- Cross-platform CI and regression tests; original MIT attribution retained.

The original upstream repository was not changed by this fork release. Future
syncs should merge upstream into a review branch and run the tests before release.
Do not replace fork-specific files wholesale or claim feature parity with raw
X API clients: this plugin uses Grok's synthesized, citation-backed discovery.

## Validation on 2026-09-22

- Local suite: 63 tests passed; Node launcher syntax and `git diff --check` passed.
- Live account search with existing OAuth: two NASA post citations, 28.068 seconds,
  one attempt, no date/handle warnings.
- Live thread research through the Node MCP launcher: root post cited, 26.356
  seconds, one attempt, no citation warnings. A cited root does not establish
  that every reply was retrieved.
- Live provider calls were limited to these two smoke checks. Rate-limit and
  token-refresh failures are covered with deterministic mocks, not forced against
  a real account. API-key preference and configurable reasoning were tested with
  mocks; the live checks retained OAuth and the existing default model.
