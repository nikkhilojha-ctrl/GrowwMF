# Mutual Fund FAQ Assistant — Problem Statement

## 1. Purpose

Build a **facts-only FAQ assistant** for mutual fund schemes, using Groww as the reference product context. The assistant answers objective, verifiable questions by retrieving information exclusively from official public sources — Asset Management Company (AMC) websites, the Association of Mutual Funds in India (AMFI), and the Securities and Exchange Board of India (SEBI).

The product prioritizes accuracy over intelligence. Every answer must be short, sourced, and free of advice, opinion, comparison, or speculation. A user should be able to trust that what they read is a restatement of an official document, not a recommendation.

## 2. Problem

Retail investors and support teams repeatedly ask the same factual questions about mutual fund schemes: expense ratio, exit load, minimum SIP, lock-in, riskometer, benchmark, and how to download statements or capital-gains reports. Those answers already exist in official documents (factsheets, KIM, SID, AMC help pages, AMFI and SEBI guidance), but they are scattered, long, and hard to scan.

Generic chatbots make this worse. They mix official facts with blogs and aggregators, calculate returns, compare funds, and drift into investment advice. That is unacceptable for a financial product surface: an unsourced or advisory answer can mislead a user and create compliance risk.

This project closes that gap with a narrow assistant that:

- Retrieves only from a curated corpus of official public documents.
- Answers only factual questions.
- Refuses advisory, comparative, and performance-calculation questions.
- Shows exactly one source link and the date sources were last updated.

## 3. Objective

Design and implement a lightweight Retrieval-Augmented Generation (RAG) assistant that:

1. Answers factual queries about a small, fixed set of mutual fund schemes.
2. Uses a curated corpus of official documents as the only knowledge source.
3. Returns concise, source-backed responses that a user can verify in one click.
4. Supports multiple independent conversation threads at the same time.

## 4. Reference context

Groww is the reference product. The assistant is a facts-only FAQ surface in that context. It is not a portfolio tool, not a fund screener, and not a substitute for scheme documents.

The assistant does not connect to a user’s Groww account. It does not look up holdings, transactions, or personal tax data. Questions about a specific person’s account are out of scope and must be refused.

## 5. Target users

| User | Need | What they should get |
| --- | --- | --- |
| Retail investor comparing schemes | A quick, checkable fact (expense ratio, exit load, minimum SIP, lock-in, riskometer, benchmark) | One factual answer, one official link, no suggestion of what to buy |
| Customer support | A consistent reply to repetitive mutual fund questions | The same sourced answer every time, with a link they can share |
| Content teams | A way to point users at official wording without rewriting it as advice | A short restatement plus the canonical page |

## 6. Scope

### 6.1 Corpus

The corpus is small and hand-curated. It is the entire knowledge boundary of the assistant.

- The AMC is **UTI Mutual Fund**.
- The scheme set is the **10 UTI direct-growth schemes** listed below. They cover index, large and mid cap, flexi-cap equity, mid cap, small cap, ELSS, hybrid, short-duration debt, and a commodity fund of funds.
- Collect **15–25** official public URLs for those schemes. The set should include:
  - Scheme factsheets
  - Key Information Memorandum (KIM)
  - Scheme Information Document (SID)
  - AMC FAQ and help pages
  - AMFI and SEBI guidance pages
  - Statement and tax-document download guides

Only these pages, and pages they officially publish, may be indexed. Third-party blogs, news sites, review sites, and aggregator websites are excluded even when they quote an AMC.

**Selected schemes (Groww reference pages)**

| Scheme | Groww page |
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

### 6.2 Questions the assistant must answer

Facts-only queries, including:

- Expense ratio of a scheme
- Exit load
- Minimum SIP amount
- ELSS lock-in period
- Riskometer classification
- Benchmark index
- How to download account statements or capital-gains reports

Answers are grounded in the retrieved passage. If the corpus does not contain the fact, the assistant says so and points to the most relevant official page. It does not fill the gap from general knowledge.

### 6.3 Response contract

Every successful answer must satisfy all of the following:

- **Length.** At most 3 sentences.
- **Citation.** Exactly one source link, and that link must be from the curated official corpus.
- **Footer.** A closing line in this form: `Last updated from sources: <date>`
- **Tone.** Factual, neutral, and verifiable. No adjectives that imply suitability (“good”, “safe”, “best”, “ideal for”).

Performance questions are a special case. The assistant does not quote returns, CAGR, or rankings, and does not compute them. It points the user to the official factsheet only.

### 6.4 Refusal handling

The assistant refuses non-factual and advisory queries. Examples:

- “Should I invest in this fund?”
- “Which fund is better?”
- Requests to compare returns, rank schemes, or recommend an allocation
- Requests that need personal data (PAN, account number, holdings, tax situation)

A refusal must:

- Be polite and clearly worded.
- Restate the facts-only limitation.
- Include one relevant educational link (for example an AMFI or SEBI investor-education page).

Refusal is a successful behavior, not an error state.

### 6.5 User interface

The interface is minimal:

- A welcome message that states what the assistant can and cannot do.
- Three example questions a user can click to start.
- A visible disclaimer: **Facts-only. No investment advice.**
- A way to open and switch between independent chat threads. Threads do not share history. An answer in one thread must not depend on another thread.

### 6.6 Multiple conversations

The assistant must handle multiple independent conversations or threads at the same time. Each thread has its own history. Retrieval and generation for one thread must not leak into another.

## 7. Constraints

### 7.1 Data and sources

- Use only official public sources: UTI Mutual Fund, AMFI, and SEBI.
- Do not use third-party blogs or aggregator websites.
- Do not invent figures. If a number is not in the retrieved source, do not state it.

### 7.2 Privacy and security

Do not collect, store, or process:

- PAN or Aadhaar numbers
- Account numbers
- OTPs
- Email addresses or phone numbers

The assistant has no login and no account linkage. If a user pastes personal identifiers, the system must not persist them and should decline to use them.

### 7.3 Content

- No investment advice or recommendations.
- No performance comparisons.
- No return calculations.
- For performance-related queries, provide a link to the official factsheet only.

### 7.4 Transparency

Responses are short, factual, and verifiable. Every answer carries one source link and a last-updated date so the user can check the underlying document.

## 8. Out of scope

The following are explicitly not part of this project:

- Personalized advice, risk profiling, or goal-based planning
- Buy, sell, or SIP execution
- Live NAV feeds, portfolio tracking, or holdings lookup
- Cross-AMC comparison or fund screening
- Return, CAGR, or rolling-return computation
- Scraping or indexing non-official sites
- User accounts, authentication, or storage of personal data
- Multi-AMC coverage beyond the single selected AMC
- Replacing SID, KIM, or factsheet documents; the assistant cites them, it does not supersede them

## 9. Architecture expectations

The implementation is a lightweight RAG pipeline:

1. **Ingest.** Fetch the curated official URLs and split them into passages, keeping the source URL and retrieval date on every chunk.
2. **Index.** Store passages so a query can retrieve the few chunks most relevant to the question.
3. **Guard.** Classify the query before or during generation. Advisory, comparative, and personal-data queries take the refusal path and never receive a generated recommendation.
4. **Generate.** Produce at most three sentences grounded in the retrieved passages, with exactly one citation.
5. **Attribute.** Append `Last updated from sources: <date>` using the retrieval date of the cited source.
6. **Threads.** Isolate conversation state per thread so concurrent chats stay independent.

The model is not the source of truth. The corpus is. Generation only compresses and cites retrieved text.

## 10. Deliverables

| Deliverable | Contents |
| --- | --- |
| README | Setup instructions, selected AMC and schemes, RAG architecture overview, known limitations |
| Disclaimer | The visible line: “Facts-only. No investment advice.” |
| Assistant | A RAG chatbot that runs multiple independent threads and follows the response and refusal contracts |

The README should name UTI Mutual Fund, list the 10 schemes and their Groww pages from section 6.1, and list the 15–25 official source URLs (or point to the corpus manifest that does).

## 11. Success criteria

The project is successful when all of the following hold:

- Factual questions about the selected schemes return the figure or rule that appears in the official source.
- Answers stay within 3 sentences and contain exactly one working citation from the corpus.
- Every answer ends with the last-updated footer.
- Advisory and comparison questions are refused politely, with the facts-only limitation and one educational link.
- Performance questions do not state or calculate returns; they link to the official factsheet.
- The interface shows the welcome message, three example questions, and the disclaimer.
- Two threads can run at once without sharing history or mixing citations.
- No personal identifiers are collected or stored.
- No non-official source is cited.

## 12. Example interactions

**Factual — in scope**

> User: What is the exit load for this scheme?
>
> Assistant: The scheme charges an exit load of 1% if units are redeemed within the period stated in the scheme documents. See the scheme factsheet for the current figure. https://…official-factsheet…
>
> Last updated from sources: 25 Oct 2026

**Performance — cite only**

> User: What is the 5-year return?
>
> Assistant: This assistant does not calculate or quote performance. The latest returns are published on the official factsheet. https://…official-factsheet…
>
> Last updated from sources: 25 Oct 2026

**Advisory — refuse**

> User: Should I invest in this fund?
>
> Assistant: This assistant shares facts from official documents only and does not provide investment advice or recommendations. For investor education, see the AMFI or SEBI resource linked here. https://…amfi-or-sebi-education…
>
> Last updated from sources: 25 Oct 2026

## 13. Known limitations

- Coverage is UTI Mutual Fund and the 10 schemes in section 6.1. Questions about other funds cannot be answered from this corpus.
- Facts go stale when an AMC updates a factsheet. The footer date tells the user when the corpus was last refreshed; it is not a live market feed.
- Official PDFs and HTML pages differ in structure. Retrieval quality depends on how cleanly those pages are parsed.
- The assistant restates published facts. It does not interpret tax law for an individual and does not confirm whether a rule applies to a specific holding.
- “Last updated” reflects the corpus refresh, not the date the regulator or AMC originally published the page, unless that publication date is itself extracted and stored.

## 14. Disclaimer

The product must display, in the interface and in the README:

> Facts-only. No investment advice.

## 15. Summary

The goal is a trustworthy, transparent, and compliant mutual fund FAQ assistant. It retrieves from a small official corpus, answers in at most three sentences, cites exactly one source, dates that source, and refuses anything that would turn a fact into advice. Accuracy and restraint are the product.
