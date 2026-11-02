# Labelling spec: ideal filters for a 10-K disclosure-change chatbot

You are labelling the IDEAL interpretation of each user question, as a careful financial analyst would read it.
Label what the question means, not what any particular system would do.

## The data the chatbot answers from

Year-over-year comparisons of SEC 10-K filings ("alignments"). Each alignment row links a disclosure in one year's
10-K to the matching disclosure in the next year's 10-K, with a change label and an LLM materiality score.

- Companies (11), lowercase tickers: bac (Bank of America), jpm (JPMorgan Chase), c (Citigroup), gs (Goldman Sachs),
  ms (Morgan Stanley), intc (Intel), amd (AMD), nvda (NVIDIA), avgo (Broadcom), qcom (Qualcomm), mu (Micron).
  Banks: bac, jpm, c, gs, ms. Chipmakers: intc, amd, nvda, avgo, qcom, mu.
- Comparison windows loaded for every company: "2023-2024" (2023 10-K vs 2024 10-K) and "2024-2025".
- 10-K Items covered: "1" (Business), "1A" (Risk Factors), "7" (MD&A), "8" (Financial Statements and notes),
  "15" (Exhibits).
- Change types: Unchanged, Modified, Reworded, Expanded, Reduced, New, Removed.
- Alignment statuses: needs_review, ai_verified, auto_matched, unmatched.
- Relationships: one_to_one, one_to_many (one disclosure split into several), many_to_one (merged), many_to_many.
- Disclosure taxonomy categories: "Cybersecurity & Data", "Technology & AI", "Supply Chain & Third Parties",
  "Regulation, Legal & Compliance", "Financial & Capital Resources", "Human Capital & Organization",
  "Operations & Capacity", "Strategy & Business Model".
- Match IDs look like bac_2023_2024_M0012 (canonical: lowercase prefix, capital M, 4 digits).
- Extra precomputed outputs: synthesis reports (per company over a span of years: executive summary, key findings,
  and financial figures such as revenue, margins, EPS, cash flow, debt computed from XBRL), and peer comparisons
  (how two or more companies disclose the same topic).

## Fields to label (one JSON object per question)

- `id`: the question id.
- `intent`: exactly one of
  - `match_detail`: asks about a specific match ID.
  - `method_disagreements`: where the semantic and lexical change-labelling methods disagree.
  - `financial_trends`: asks for financial FIGURES (revenue, margins, net income, EPS, cash flow, debt...) and how they moved.
    Not for questions about what a company SAYS in its text about a financial topic (that is topic_search).
  - `peer_comparison`: how two or more companies (or a company vs its peers) disclose/discuss the same topic.
  - `compare_companies`: compares change PATTERNS/statistics (how much / what kind of change) across companies or periods.
  - `low_confidence`: about the materiality model's confidence scores.
  - `summary`: summarise / overview / key changes / highlights for a company and period.
  - `count`: how many.
  - `distribution`: breakdown or most common types of change.
  - `material_changes`: the most material / significant / important changes.
  - `topic_search`: what changed about a specific subject (climate, export controls, tariffs...).
  - `list_changes`: list disclosures matching structural filters (change type, item, status, relationship).
  - `custom_sql`: none of the above.
- `intent_alternatives`: list of other intents that would also be a reasonable reading (often empty).
- `companies`: list of tickers the question restricts to (empty list = no restriction). "its peers" adds nothing
  unless peers are named. Map nicknames (Citi, BofA, Goldman) to tickers.
- `windows`: list of comparison windows the question covers, from {"2023-2024", "2024-2025"}.
  Empty list = no year restriction (all windows).
  Rules: "the 2024 10-K"/"in 2024"/"FY2024"/"for 2024" = the 2024 filing compared with the prior one = ["2023-2024"].
  "2025" = ["2024-2025"]. "2023" alone (the 2023 filing vs the next) = ["2023-2024"].
  "from 2023 to 2025"/"between 2023 and 2025" = both. "between 2023 and 2024" = ["2023-2024"].
  "since 2023" = both. "latest"/"most recent" = ["2024-2025"].
- `items`: list of Items the question restricts to ("risk factors" = 1A, "MD&A" = 7, "financial statements/notes" = 8,
  "business section" = 1, "exhibits" = 15). Empty = all.
- `change_types`: change types the question restricts to ("added" = New, "removed/dropped" = Removed,
  "shortened" = Reduced, "expanded" = Expanded, "rewrote/reworded" = Reworded, "unchanged" = Unchanged).
  Generic "changes"/"changed" restricts nothing. Empty = all.
- `taxonomies`: taxonomy categories, only when the subject clearly falls in one (e.g. cybersecurity -> "Cybersecurity & Data",
  AI -> "Technology & AI", supply chain -> "Supply Chain & Third Parties", employees/talent -> "Human Capital & Organization",
  manufacturing capacity -> "Operations & Capacity", capital requirements/liquidity -> "Financial & Capital Resources",
  regulation -> "Regulation, Legal & Compliance"). Empty otherwise.
- `statuses`: alignment statuses ("flagged for review" = needs_review). Empty = all.
- `relationships`: relationship types. Empty = all.
- `match_ids`: canonical match IDs mentioned.
- `min_materiality`: number 0-1 if the question gives a materiality threshold, else null.
- `max_confidence`: number 0-1 if the question gives an upper confidence threshold (60% -> 0.6), else null.
- `limit`: integer if the question asks for a specific number of results ("top 5"), else null.
- `topic`: short subject phrase if the question is about a specific subject (e.g. "export controls"), else null.
- `note`: one short sentence when the question is ambiguous, explaining your reading. Otherwise "".

## Follow-ups

Some items have a `context` (the previous question in the same conversation). Label the follow-up with the full
meaning it has in that conversation (carry over company/years/items/intent from the context where the follow-up
implies them; replace what it changes).
