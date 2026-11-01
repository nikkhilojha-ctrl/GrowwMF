# Mutual Fund FAQ Assistant

**Facts-only. No investment advice.**

This is a facts-only FAQ for ten UTI Mutual Fund direct-growth schemes. It answers expense ratio, exit load, minimum SIP, ELSS lock-in, riskometer, benchmark, and statement or capital-gains download questions from official UTI, AMFI, and SEBI pages. Advice, comparisons, and return calculations are refused. Every reply has at most three sentences, one official citation, and a line `Last updated from sources: <date>`.

The assistant stores no PAN, Aadhaar, account numbers, OTPs, emails, or phone numbers. It has no login. If a message contains one of those identifiers, the stored text is `[removed: personal data]`.

## Schemes

The AMC is UTI Mutual Fund. Groww links are reference pages for the sidebar. They are not fetched, indexed, or used as citations.

| Scheme | Groww reference |
| --- | --- |
| UTI Nifty Fund Direct Growth | https://groww.in/mutual-funds/uti-nifty-fund-direct-growth |
| UTI Small Cap Fund Direct Growth | https://groww.in/mutual-funds/uti-small-cap-fund-direct-growth |
| UTI Large & Mid Cap Fund Direct Growth | https://groww.in/mutual-funds/uti-large-mid-cap-fund-direct-growth |
| UTI Equity Fund Direct Growth | https://groww.in/mutual-funds/uti-equity-fund-direct-growth |
| UTI Unit Scheme 1986 Mastershare Direct Growth | https://groww.in/mutual-funds/uti-unit-scheme-1986-mastershare-direct-growth |
| UTI Mid Cap Fund Direct Growth | https://groww.in/mutual-funds/uti-mid-cap-fund-direct-growth |
| UTI Long Term Equity Fund (Tax Saving) Direct Growth | https://groww.in/mutual-funds/uti-long-term-equity-fund-tax-saving-direct-growth |
| UTI Balanced Fund Direct Growth | https://groww.in/mutual-funds/uti-balanced-fund-direct-growth |
| UTI Short Term Fund Direct Growth | https://groww.in/mutual-funds/uti-short-term-direct-growth |
| UTI Silver ETF Fund of Fund Direct Growth | https://groww.in/mutual-funds/uti-silver-etf-fof-direct-growth |

## Official sources

The corpus is the 17 allowlisted URLs in [corpus/manifest.yaml](corpus/manifest.yaml) (UTI scheme pages, the factsheet and SID libraries, statement help, and one AMFI page and one SEBI page). Scheme ids and Groww reference URLs live in [corpus/schemes.yaml](corpus/schemes.yaml). Education links used by refusals live in [corpus/education.yaml](corpus/education.yaml).

## Setup

Python 3.11 or newer. From the repository root:

```text
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m app.ingest
uvicorn app.main:app
```

On macOS or Linux, activate with `source .venv/bin/activate` instead of `.venv\Scripts\activate`.

`python -m app.ingest` needs a network connection. It fetches the manifest, replaces chunks for those URLs, and leaves existing threads in place. Then open http://127.0.0.1:8000/ for the page and http://127.0.0.1:8000/api/health for the status check. After a full ingest of the current manifest, health shows `"schemes": 10` and `"documents": 17`.

Tests do not need a network connection or an API key. From the repository root:

```text
pytest
```

Copy `.env.example` to `.env` only if you want to set variables. Leave `GROQ_API_KEY` empty to run with no chat model. Structured facts and refusals still run. A key is never logged.

| Variable | When blank |
| --- | --- |
| `GROQ_API_KEY` | No chat model. Facts and refusals still answer |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` |
| `DATABASE_PATH` | `data/assistant.db` |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` |

## Architecture

Each thread has its own messages and active scheme. There is no process-wide current scheme.

1. **Ingest.** Fetch the allowlisted manifest, parse HTML and PDF, and store chunks and labeled fact spans with the retrieval time.
2. **Guard.** Classify the message before any answer is written. Personal data, advice, comparisons, performance questions, and funds outside these ten schemes use a fixed template. Those paths do not call the chat model.
3. **Retrieve.** For a known field, return that scheme’s fact span. Otherwise search chunks for the active scheme and shared official pages.
4. **Extract or generate.** Quote the fact span unchanged. For prose, call Groq when `GROQ_API_KEY` is set, or copy a short excerpt of the best chunk when it is not.
5. **Validate.** Keep the reply only when it has one to three sentences, one allowlisted citation, figures copied from the cited span, and one footer. Anything else is replaced with a safe template.
6. **Per-thread state.** The user and assistant rows, the footer date, and the active scheme are written in one transaction for that thread id.

Groq is the chat provider. The default model is `llama-3.3-70b-versatile`. An unset `GROQ_API_KEY` still answers structured facts and refusals.

## Limitations

- Coverage stops at these ten UTI schemes. Other funds, stocks, and IPOs are refused.
- `Last updated from sources` is the ingest time of the cited URL. It changes only when ingest runs again. It is not a live NAV or a live factsheet feed.
- PDF and HTML text layers decide what can be quoted. A scanned factsheet with no text layer produces no fact row, and the answer is `not_in_corpus` rather than a guessed number. Expense ratio and minimum SIP are not labeled spans on the current UTI pages (the TER figure is filled in the browser, and minimum SIP is not a separate label). Those questions are answered from a retrieved passage when one scores high enough, and otherwise as `not_in_corpus`. Exit load, benchmark, riskometer, lock-in, and minimum lump sum are quoted when the page contains the span.
- The assistant quotes published rules. It does not decide whether a lock-in or exit load applies to a particular folio.
- Statement and capital-gains steps are only as complete as the UTI, AMFI, or SEBI page in the manifest. Registrar sites are outside the allowlist and are not fetched.
- Thread listing is local and unauthenticated. That matches a single-operator tool and is the wrong shape for a public multi-tenant deployment.
