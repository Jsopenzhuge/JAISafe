<p align="center">
  <img src="logo.png" alt="JAISafe — Local Privacy Boundary for AI Agents" width="620">
</p>

<p align="center">
  <a href="README.md">简体中文</a> · <a href="README.en.md">English</a>
</p>

<h1 align="center">Keep your local context local</h1>

<p align="center">
A <strong>local privacy boundary</strong> for AI coding agents (Claude Code, Cline, Cursor, custom agents…)
</p>

---

JAISafe runs between your agent and the model provider. It replaces the **local identity information**
in outbound requests — absolute paths, API keys, database connection strings, private keys, sensitive
filenames — with opaque handles before anything leaves your machine, and **restores the real values**
in the response on the way back.

Your client needs no changes. Your agent never knows this happened.

```
        Local (trusted)                      │              Upstream (untrusted)
                                            │
  C:\Users\me\proj\secret\layoff.pdf   ────┼──▶  [WORKSPACE_ROOT_1]\secret\[SENSITIVE_DOC_1].pdf
  ghp_9fK2… (real token)               ────┼──▶  ghp_Xy3k… (same length, same prefix)
  postgres://svc:hunter2@db.corp.internal ─┼──▶  postgres://vq3f:9Kd2…@db-a7f2.svc.synthetic
                                            │
  C:\Users\me\proj\secret\layoff.pdf   ◀───┼────  handles echoed by the model are restored
```

> This is not a multi-tenant API gateway like NewAPI / One-API. Protocol translation, channel routing
> and failover exist so that the privacy boundary can sit at **one single point** — see
> "Why a gateway".

---

## The problem it solves

AI coding agents stuff a lot of **local context** into every request: absolute file paths, `grep`
output, stack traces, `.env` fragments, shell history, Git branch names. Once sent upstream, that
content has left your machine.

The issue is not "the model can see my code" — that is what you asked it to do. The issue is
**everything around your code**:

- your username, home directory layout, internal directory naming
- **sensitive filenames** in the project (`layoff_plan_2026.pdf`, `acme-acquisition-2026/`)
- real secrets sitting in environment variables (paste a `.env` and it is all gone)
- database connection strings, internal hostnames, private keys

None of this helps the model complete the task, yet upstream it gets logged, may enter training data,
and may show up in audit trails. JAISafe does one specific thing: **strip that layer before it leaves**.

A real agent request, before and after:

| | Client-side (local) | Actually sent upstream |
| --- | --- | --- |
| Path | `C:\Users\me\projects\myapp\src\server.py` | `[WORKSPACE_ROOT_1]\src\server.py` |
| Credential | `ghp_9fK2mQ…` (real 36-char token) | `ghp_Xy3kPq…` (same length, same prefix, HMAC-derived) |
| Filename | `layoff_plan_2026.pdf` | `[SENSITIVE_DOC_1].pdf` |
| Host | `vault.acme-corp.internal` | `host-a7f2q9.corp.internal` |

The model can still reason that the file lives under `src/`, that this is a GitHub token, that this is
an internal host. It **cannot tell who you are, what your machine looks like, or what your keys are**.

---

## What it protects — and what it does not

Privacy tooling turns into security theatre very easily, so this section comes first.

| ✅ Protected | ❌ Not protected |
| --- | --- |
| Absolute local paths (drive letter / UNC / POSIX), home directory layout | **The code, prompts and business content you deliberately send to the model** |
| Credentials in env vars and source (keys / JWTs / connection strings / private keys) | Business information upstream can infer (project type, stack, code style) |
| Sensitive filenames and directory names (configurable keywords) | Request metadata: model name, timestamps, token counts, stream duration, `user` field |
| Email addresses, internal hostnames | What you send when you call the provider directly from **other** tools |
| The above, when echoed back by the model (restored on the response side, invisible to the client) | How upstream uses the data once it has left your machine |

**To be explicit: this tool does not blind the model to your code.** Its goal is to strip *local
identity*, not to encrypt your business content. If you need "source code must never leave the
machine", what you want is a local model, not this.

Two further premises:

- **The machine running JAISafe must be one you trust.** It *is* the trust boundary, not a layer of
  encryption inside one.
- **The mapping store is plaintext on your local disk** (see "Threat model and costs").

---

## How it works

OpenAI Chat Completions is used internally as the canonical format. An inbound request is parsed into
that form, masked, then translated into the upstream protocol. The response walks the same path in
reverse, ending with restoration.

```
Client                    JAISafe                                     Upstream
  │                        │                                           │
  │  /v1/messages          │                                           │
  ├───────────────────────▶│  1. parse protocol → canonical            │
  │                        │  2. mask: paths / credentials → handles   │
  │                        │     ↳ handle↔value stored in session table│
  │                        │  3. canonical → upstream protocol         │
  │                        ├──────────────────────────────────────────▶│ sees handles only
  │                        │◀──────────────────────────────────────────┤
  │                        │  4. upstream protocol → canonical         │
  │                        │  5. restore: handles → real values        │
  │◀───────────────────────┤                                           │
  │  identical to unmasked │                                           │
```

### Why a gateway, not a client-side change

| | Patch each client / use a library | Put it in a gateway (this project) |
| --- | --- | --- |
| Integration cost | Once per agent | **Once, covering every client** |
| Response restoration | The agent must implement handle resolution | **The gateway sees the response; the client sees nothing** |
| Switching providers | Rewrite for each one | Protocol translation happens inside |
| Client upgrades | Must keep up | Unaffected |

The second row is the crux. The reference implementation,
[SlotGuard](https://openreview.net/forum?id=waW0KyByrv), is a Rust library that must be integrated
into every agent runtime, and **it only rewrites outbound traffic — it never restores** (its `raw_for`
is called from unit tests and nowhere else in the repository), because a library cannot see the
response. A gateway can, which is what makes a zero-change client possible.

---

## Quick start

```bash
pip install -r requirements.txt
python run.py                      # http://127.0.0.1:8000 by default
```

On Windows you can simply double-click `start.bat`.

### 1. Open the WebUI and change the default password

<http://127.0.0.1:8000/> → default admin password is `admin` → **change it on the Settings page
immediately**.

### 2. Add a channel

Channels → Add channel. Fill in the upstream Base URL and API key:

| Provider | Base URL |
| --- | --- |
| OpenAI | `https://api.openai.com/v1` |
| Anthropic | `https://api.anthropic.com` |
| DeepSeek | `https://api.deepseek.com/v1` |
| SiliconFlow | `https://api.siliconflow.cn/v1` |
| Local Ollama | `http://127.0.0.1:11434/v1` |

> When the upstream is a **local model**, set that channel's masking to `off`. The content never left
> the machine anyway, so masking only adds false positives and needless plaintext mapping on disk.

### 3. Point your agent at JAISafe

Change the agent's `base_url` and `api_key`. Nothing else:

```bash
# Claude Code
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=sk-jai-xxxx
```

### 4. Configure the scope to protect

Masking → fill in the **path root** (your code workspace), for example:

```
C:\Users\me\projects\myapp
```

Paths under a configured root keep their full relative part (best readability); paths under the home
directory that match no root have their middle segments collapsed.

### 5. Dry run first, enforce later

This is the **recommended rollout path**. Do not skip steps:

| Step | Mode | What you get |
| --- | --- | --- |
| 1 | `dry_run` | Requests go out unchanged, but the log records what *would* have been masked |
| 2 | Paste a few real prompts into "Rule preview" | Confirm what gets matched and that no code was mangled |
| 3 | `enforce` | Real masking, with automatic restoration in responses |

![Masking settings](docs/screenshots/mask.png)

---

## Masking coverage

| Type | Example | Replaced with | Basis |
| --- | --- | --- | --- |
| Workspace / repo root | `C:\ws\proj\src\a.py` | `[WORKSPACE_ROOT_1]\src\a.py` | configured roots |
| Home directory (middle collapsed) | `/home/u/a/b/c/d/e.txt` | `[USER_HOME_1]/[SENSITIVE_PATH_SEGMENT_1]/d/e.txt` | home directory |
| Sensitive filename | `layoff_plan_2026.pdf` | `[SENSITIVE_DOC_1].pdf` | keywords + year/`for_` shape |
| Sensitive directory segment | `acme-acquisition-2026/` | `[SENSITIVE_PATH_SEGMENT_2]/` | keywords + separators/digits |
| API keys | `sk-… ghp_… AKIA… xoxb-… hf_… glpat-… npm_… AIza…` | same length, same prefix | regex + format-preserving synthesis |
| JWT / OAuth | `eyJ…eyJ…xxx` | three segments, same lengths, still starting with `eyJ` | regex |
| Database strings | `postgres://u:p@host/db` | scheme / port / path preserved | regex |
| Private key PEM | `-----BEGIN … PRIVATE KEY-----` | PEM frame preserved, base64 body replaced | regex |
| Email / PII | `dave@team.example` | fake address keeping the TLD | regex |
| Internal hostnames | `vault.acme-corp.internal` | suffix such as `.internal` preserved | suffix list |

**Synthetic values are HMAC-derived and do not depend on the bytes of the original.** This is
deliberate: if a synthetic value were a transformation of the original, an attacker who guessed the
function could invert it from the upstream-visible value alone. The derivation inputs are
"session key + category + counter", so the same real value maps stably to the same synthetic within a
session — otherwise the model would see a different path every turn and the task would break.

**By default only paths under the configured roots / home directory are masked.** Public paths such as
`/usr/bin/python` or `/etc/hosts` are left alone: they carry no identity information, and rewriting
them only adds noise. Enable "mask absolute paths outside the roots" for a more aggressive policy.

The built-in sensitive-filename keyword list covers English and Chinese terms
(`layoff` / `salary` / `contract` / 裁员 / 薪资 / 机密 …) and can be overridden in the UI.

---

## Three modes

| Mode | Upstream receives | Purpose |
| --- | --- | --- |
| `off` (default) | original text | Disabled. Behaviour is exactly as if this layer did not exist |
| `dry_run` | **original text** | Records matches without rewriting. Use it to validate the rules |
| `enforce` | handles | Real masking, restored before the response returns |

The default is `off`, not `enforce`: silently changing what a running agent sends is too risky — if
restoration were ever off by a byte, the agent would go execute a fake path. Follow the rollout order
in "Quick start" step 5.

Per-channel override is available: `inherit` (follow global) / `off` (local model) / `enforce`
(external provider).

---

## Audit: know exactly what went out

A privacy tool that cannot answer "which parts of that request actually left my machine?" is neither
verifiable nor accountable. The **Masking** tab on every log entry provides:

- match counts (N paths / M credentials, broken down by type)
- **client-side original** and **what was actually sent upstream**, side by side
- the **handle↔value mapping** created by that request (blurred by default, click to reveal)
- reverse lookup of a handle back to its real value
- a "what would have been masked" preview in dry-run mode

![Masking trace in the log](docs/screenshots/log_mask_tab.png)

---

## Threat model and costs

### The inherent cost of reversible masking: the mapping is plaintext on disk

For responses to be restorable, the gateway must remember "handle → real value". It is a stateless
request handler, so it cannot keep this in memory alone — the mapping is **persisted in
`data/gateway.db`, in plaintext**.

This is not an implementation defect; it is a necessary consequence of reversible masking. It means:

- `data/gateway.db` becomes **a file containing plaintext credentials** — restrict access to it
  (NTFS ACL / `chmod 600`)
- the admin panel's default password is `admin`; **change it before enabling masking**
- the log page records the raw client body by default (turn off `log_request_body` in Settings)
- the Masking page offers a separate retention period and a one-click wipe

**To avoid persistence entirely**, turn off "store the handle↔value mapping". The cost is that
responses cannot be restored and the log cannot be traced — fine if all you care about is that
nothing leaks outbound and you do not care about the client experience.

### What should not be misread

- **JAISafe itself is the boundary.** It handles all request content in plaintext. If it is
  compromised or misconfigured, it stops being a protection layer. Do not treat it as a zero-trust
  encryption scheme.
- **It does not hide that you use AI.** Model name, timestamps, token counts and request patterns
  remain visible.
- **It does not stop upstream inference.** The masked structure still reveals stack and project shape.

---

## Measured results

The algorithm is ported from SlotGuard and differentially aligned against its **public corpus**
(200 sessions / 852 credentials, covering direct / embedded / split / derived / cross-turn exposure).

Format fidelity of credential synthesis (`tests/mask_test.py`):

| Category | Samples | Length match | Prefix match | Same-class detectable |
| --- | --- | --- | --- | --- |
| api_key | 200 | 1.00 | 1.00 | 1.00 |
| oauth_token | 200 | 1.00 | 1.00 | 1.00 |
| cryptographic_key | 52 | 1.00 | 1.00 | 1.00 |
| database_credential | 200 | 0.27 | 1.00 | 1.00 |
| pii_identifier | 200 | 0.00 | 1.00 | 1.00 |

`api_key` scores 1.00 across all three metrics, matching SlotGuard's published
`format_validity.json`. Connection strings and emails change length because synthesis replaces the
username/password/host — that is by design.

Residual leakage under realistic conditions (no answer key pre-seeded):

| Exposure | This implementation | SlotGuard paper |
| --- | --- | --- |
| direct | 0 / 200 | 0 / 200 |
| embedded | 0 / 200 | 0 / 200 |
| derived | 0 / 200 | 0 / 200 |
| cross-turn | 0 / 52 | 0 / 52 |
| **split** | **105 / 200** | 0 / 200 ⚠️ |

**`split` is the capability limit of this implementation — and the "numbers trap" of the reference
implementation.** When a token is cut in half and separated by quotes or whitespace (`"AAA" "BBB"`),
it cannot be recognised without already knowing the answer. SlotGuard reports 0/200 only because its
experiment harness **pre-registers the two halves as graph nodes using the ground truth from the
answer key** before running (`experiments/src/main.rs:6235`). A real gateway has no such foresight.

---

## Known limits

- **Spelling variants and Base64-encoded credentials**: the regexes match literally, so encoded or
  transformed values are not recognised.
- **Split credentials**: see the table above.
- **Under `enforce`, streaming responses are no longer byte-transparent.** Streaming restoration must
  happen at the level of structured chunks, so the path becomes "parse → restore → re-render".
  A few upstream-specific fields (Anthropic's `signature_delta`, `ping`, …) are no longer passed
  through. This is the price of restoration and applies only in `enforce` mode.
- **JSON Schema bodies of tool definitions are not masked** (only `description` is), to avoid breaking
  schema keywords.
- **Embeddings are masked but never restored**: vectors cannot be restored, and what the client
  actually wants is the embedding of the masked text.

### Why streaming restoration is hard

Handles are ordinary strings and get split across SSE chunks (`[WORKSPACE` + `_ROOT_1]/src`). The
implementation maintains a sliding window over the set of handle prefixes and only releases characters
to the client once the tail of the buffer can no longer be a prefix of any handle. Separately,
`tool_calls` arguments are JSON fragments, so replacement values are JSON-escaped first — otherwise a
Windows path's `\U` would inject an invalid escape and the client would fail to parse it.

---

## Supported protocols

For the privacy boundary to sit at one point, it has to speak both the client's and the upstream's
protocol. This part is a means, not the end.

### Endpoints

| Method | Path | Protocol |
| --- | --- | --- |
| POST | `/v1/chat/completions` | OpenAI Chat Completions |
| POST | `/v1/messages` | Anthropic Messages |
| POST | `/v1/messages/count_tokens` | Anthropic token estimate |
| POST | `/v1/responses` | OpenAI Responses API |
| POST | `/v1/completions` | OpenAI Completions (legacy) |
| POST | `/v1/embeddings` | Embeddings (masked, then forwarded) |
| GET | `/v1/models` | Model list |
| GET | `/health` | Health check |

Channel types cover OpenAI-compatible / Anthropic Messages / OpenAI Responses. **Any endpoint ↔ any
upstream** is translated automatically, including:

- **SSE event-level streaming translation** (not concatenation): `chat.completion.chunk` ↔
  `message_start`/`content_block_delta` ↔ `response.output_text.delta`
- **Full tool-calling chain**: `tool_calls` ↔ `tool_use`/`tool_result` ↔ `function_call`, including
  incremental streaming arguments
- **Reasoning effort**: `reasoning_effort` ↔ `reasoning.effort` ↔ `thinking.budget_tokens`
- **Same-format passthrough**: when the endpoint and upstream protocols match, request bodies and
  response bytes are **forwarded verbatim** with no needless rewriting (except under `enforce` with
  streaming — see "Known limits")

### Client setup

```python
# OpenAI SDK
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-jai-xxxx")
client.chat.completions.create(model="gpt-4o-mini",
                               messages=[{"role": "user", "content": "hello"}])
```

```python
# Anthropic SDK (fully translated even when the upstream is an OpenAI-compatible channel)
from anthropic import Anthropic
client = Anthropic(base_url="http://127.0.0.1:8000", api_key="sk-jai-xxxx")
client.messages.create(model="claude-3-5-sonnet-20241022", max_tokens=1024,
                       messages=[{"role": "user", "content": "hello"}])
```

---

## Other features

| Feature | Description |
| --- | --- |
| **Channel management** | base_url / api_key / model allow-list / model mapping / extra headers / timeout / per-channel masking policy |
| **Failover** | Retries by priority; connection errors, timeouts and 4xx/5xx fall through to the next channel; the attempt count is logged |
| **Gateway keys** | `Authorization: Bearer` or `x-api-key`; authentication is enforced automatically once an enabled key exists |
| **Request visibility** | Every request records client body, upstream body, upstream response, raw SSE, duration and tokens |
| **Playground** | Built-in console: pick a protocol, stream live output, generate a curl command |
| **Zero dependencies** | No Redis / MySQL / Node — one Python process and one SQLite file |

---

## Screenshots

| Overview | Channels |
| --- | --- |
| ![Overview](docs/screenshots/overview.png) | ![Channels](docs/screenshots/channels.png) |

| Playground | Settings |
| --- | --- |
| ![Playground](docs/screenshots/playground.png) | ![Settings](docs/screenshots/settings.png) |

---

## Configuration reference

Configured visually on the Masking page; stored as settings:

| Key | Default | Description |
| --- | --- | --- |
| `mask_mode` | `off` | `off` / `dry_run` / `enforce` |
| `mask_paths` `mask_credentials` | `1` | Toggle path and credential masking independently |
| `mask_workspace_roots` `mask_repo_roots` | empty | One absolute path per line; repo roots take precedence |
| `mask_include_home` | `1` | Use the home directory as a fallback root (middle segments collapsed) |
| `mask_preserve_segments` | `3` | Trailing segments kept when collapsing |
| `mask_outside_roots` | `0` | Also mask absolute paths outside the roots |
| `mask_credential_mode` | `fps` | `fps` (format-preserving) / `placeholder` (`[ACCESS_TOKEN_1]`) |
| `mask_sensitive_keywords` | built-in EN+ZH list | Sensitive filename / directory keywords |
| `mask_internal_suffixes` | `.internal,.intranet,.corp,.local,.lan` | Internal hostname suffixes |
| `mask_propagate` | `1` | Replace known values even when the regex misses them (cross-turn) |
| `mask_session_source` | `auto` | Handle scope: `X-Mask-Session` header → body `user` → API key → IP |
| `mask_store_map` | `1` | Persist the mapping; **turning it off disables restoration** |
| `mask_retention_days` | `7` | Independent retention for the masking mapping |

Stability of the handle scope (session) matters more than isolation: if the same real value got a
different handle in different requests, the model would see a different path each turn and the task
would fail outright. The default `auto` resolves in the order "header → body `user` → API key → IP".

---

## Relationship to SlotGuard

The algorithm is ported from [SlotGuard](https://openreview.net/forum?id=waW0KyByrv) (Rust, research
prototype), with trade-offs made for the gateway setting:

| | SlotGuard | This project |
| --- | --- | --- |
| Form | Rust crate + CLI, no HTTP service | Local service embedded directly in the agent's path |
| Integration | Once per agent runtime | Once, covering every client |
| **Response restoration** | **None** (`raw_for` is only called by unit tests) | Yes, including a streaming sliding window |
| Windows paths | **Unsupported** (regex matches POSIX absolute paths only) | Drive letters / UNC / both slash styles |
| SEG graph machinery | Present, but `edges`/`redirections` are dead code | Not ported; only the "node set + substring scan" that actually runs |
| Email / internal host | Separate slot mechanism | Routed through FPS, fewer mechanisms |
| Path masking scope | Rewrites every absolute path unconditionally | Only under configured roots / home, far fewer false positives |
| Paper harness | 6,400 lines of experiment code | Not ported |

---

## Project layout

```
llm-gateway/
├── run.py                    # entry point
├── start.bat / start.sh
├── requirements.txt
├── app/
│   ├── main.py               # FastAPI app and routes
│   ├── relay.py              # request handling: auth / routing / masking / restore / forward / logging
│   ├── admin.py              # admin API
│   ├── db.py                 # SQLite storage layer
│   ├── masking/              # ★ the local-context privacy boundary
│   │   ├── patterns.py       #   credential regexes and categories
│   │   ├── fps.py            #   HMAC derivation + format-preserving synthesizers
│   │   ├── paths.py          #   cross-platform paths (POSIX / drive / UNC)
│   │   ├── engine.py         #   path abstraction + credential substitution + restoration
│   │   ├── slots.py          #   session-scoped handle table (persisted in SQLite)
│   │   ├── walk.py           #   message-tree traversal for four protocols
│   │   ├── stream.py         #   streaming restoration sliding window
│   │   └── config.py         #   settings assembly and session identity
│   ├── formats/              # protocol parsing and translation (the means)
│   └── static/               # WebUI (plain HTML/CSS/JS, no build step)
├── tests/
│   ├── mock_upstream.py      # mock upstream for all three protocols
│   ├── mask_test.py          # masking engine + corpus differential
│   ├── smoke_test.py         # in-process end-to-end (format matrix / masking round-trip / reasoning)
│   └── live_check.py         # live HTTP check (non-destructive)
└── data/gateway.db           # created at runtime (holds the mapping — mind the permissions)
```

---

## Tests

```bash
# Masking engine: path abstraction / FPS determinism / round-trip / stream chunking / corpus differential
python tests/mask_test.py                       # 59 assertions

# In-process end-to-end: 4 endpoints × 3 upstreams × streaming/non-streaming, incl. masking round-trip
python tests/smoke_test.py                      # 389 assertions

# Live HTTP check (non-destructive; only creates/removes __livetest__-prefixed resources)
python tests/mock_upstream.py                   # terminal A
python run.py --port 18000                      # terminal B
python tests/live_check.py                      # terminal C
```

The masking section of `smoke_test.py` asserts that **the upstream body contains neither the original
paths nor the credentials**, that **the client receives the restored real values**, and that **the log
retains a traceable mapping**.

---

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `JAI_HOST` / `JAI_PORT` | `127.0.0.1` / `8000` | Listen address |
| `JAI_DATA_DIR` | `./data` | Data directory (holds the handle mapping — mind the permissions) |
| `JAI_ADMIN_PASSWORD` | `admin` | Admin password used at first initialisation |
| `OPENAI_BASE_URL` + `OPENAI_API_KEY` | — | Creates a default channel on first start when none exists |

---

## Deployment advice

- **Bind to loopback only.** That is the default (`127.0.0.1`). If you must serve a LAN, change the
  admin password first, enable mandatory key validation, and put it behind a reverse proxy.
- **Restrict access to `data/`.** It contains all your relay keys and the handle mapping, in plaintext.
- **Keep `data/` out of version control and backups.** Already covered by `.gitignore`.
- **Set a sensible `mask_retention_days`.** Expired mappings are purged, shrinking the plaintext window.
- **Set masking to `off` for channels pointing at local models.** Unnecessary masking only causes harm.
- **Roll out with `dry_run` for one full task cycle** before switching to `enforce`, to confirm no code
  identifiers are mangled.

---

## FAQ

**Q: With masking on, can the model still do useful work?**
Probably yes — but understand the boundary of what I verified: **I validated format fidelity, not task
success rate.** Paths keep a readable trailing portion and credentials keep their length and prefix,
so the model can still locate files and recognise "this is an API key"; see "Measured results" for the
three format metrics over 200 sessions / 852 credentials. No masking scheme can guarantee downstream
tasks are 100% unaffected — **which is exactly why `dry_run` is the recommended first step**: run your
own real prompts through it and confirm it does not touch identifiers the model relies on.

**Q: Why weren't my code identifiers masked?**
By design. Masking targets **local identity information** (paths, credentials, sensitive filenames) and
does not touch code content. `abstract_components` only abstracts sensitive directory segments and
sensitive filenames; ordinary source filenames and symbol names pass through unchanged — masking them
would strip the model of all context.

**Q: Is what the client receives under `enforce` identical to before?**
Yes. Handles in the response are restored to real values, so the client cannot tell the difference.
The one exception is that streaming responses are no longer byte-transparent (see "Known limits") —
semantically equivalent, but upstream-specific fields may be dropped.

**Q: Can I mask without restoring?**
Yes. Turn off "store the handle↔value mapping" — nothing leaks outbound, but the client will see
handles. Suitable for pure outbound auditing.

**Q: Upstream returns 404 / 401, yet the same config works in another tool?**
Check whether the Base URL duplicates `/v1`. This program appends `/v1` automatically, and does not
double-append when the base URL already ends in `/v1`, `/v2`, `/v1beta`, or the target endpoint itself.

**Q: Why are the token counts in the log zero?**
The upstream did not return `usage`. Streaming requests require upstream support for
`stream_options.include_usage`; some compatible services do not, in which case turn off "request usage
statistics from upstream on streaming requests" for that channel.

**Q: Anthropic clients report `max_tokens: field required`.**
The Anthropic protocol requires it. When an OpenAI-protocol request targets an Anthropic upstream, the
gateway fills in `4096` by default; pass an explicit `max_tokens` if the target model's limit is lower.

**Q: What happens when several channels match the same model?**
They are tried in descending priority order and the first success is returned; on failure the next one
is attempted automatically, and the log records the number of retries. Note this means **a single
request can be sent to more than one upstream** — if exposure surface matters to you, make sure only
the channels you genuinely need are enabled.

---

## License

[Apache License 2.0](LICENSE) © 2026 Jsopenzhuge

Free for commercial and non-commercial use, including modification and redistribution, provided the
copyright and license notices are retained (Section 4). Apache-2.0 was chosen over MIT for its
**explicit patent grant** (Section 3) — for a project handling credentials and privacy boundaries, the
patent terms between contributors and users are worth stating plainly.

This repository contains no `NOTICE` file, so redistribution carries no additional attribution
obligation; keeping `LICENSE` is sufficient.
