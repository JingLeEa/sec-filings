# YUVIKA: home page redesign and chatbot page

**Maintainer:** Yuvika. This note covers only the new home page and the chatbot page. The rest of the dashboard is described in `YUVIKA_ui_dashboard.md`; the chatbot engine is described in `AARAV_chatbot.md`.

## 1. What changed and why

The old home page was a summary page: a few charts and counts with little explanation. It now works as a landing page for someone who has never seen the app.

| Section | What the reader gets |
| --- | --- |
| Hero | One-sentence description and a five-step route through the app (prepare, explore, read the evidence, get the summary, ask questions). |
| "What would you like to do?" | One coloured card per page, each with three bullet points and a button. A page that does not exist on the current branch is skipped. |
| Ask box | Type a question or pick an example; the chatbot page opens and answers it. |
| Snapshot | Four numbers (matched groups, with a change, material changes, need review). Each says in plain words what it counts, gives the share of all groups, and has a button that opens the explorer with the matching filters already set. |
| Charts | Type of change (one colour per label), part of the filing, and topic. |
| Key terms | Tabs explaining change labels, match status and SEC items. |
| Good to know | Three short warnings: labels are AI output, "no counterpart" is not proof of absence, years are fiscal years. |

After a manual review is saved, the numbers read from the same cached loaders as the other pages. Press **Reload data** in the sidebar if they look old (the review refresh clears the cache itself).

## 2. Chatbot page

`app/pages/6_Chatbot.py` wraps `sec_disclosure.chatbot.Chatbot`. Nothing in the chatbot engine was changed.

- Two sidebar switches: AI-written answers, and meaning-based search. With both off the page still answers, from templates.
- Example questions for the company selected in the sidebar.
- Each answer shows the type of question, whether it was AI-written or a template, whether the citations were checked, and the time taken.
- Cited match IDs (for example `amd_2024_2025_M0001`) can be opened on the Side by Side page with one click.
- "Where this came from" lists the database rows used; "The database query" shows the SQL.
- The sidebar shows what the bot remembers for follow-ups (company, years, Item). "Clear conversation" resets it.
- If the database or chatbot code is missing the page shows setup steps, not a traceback.
- One read-only database connection is shared by all browser sessions; each session has its own `Chatbot` so follow-up context is not mixed between users.

## 3. Files

| File | Responsibility |
| --- | --- |
| `app/Home.py` | The new home page (replaces the old one). |
| `app/sec_ui/landing.py` | Colours, the feature cards, label and status explanations, CSS. No page logic. |
| `app/sec_ui/chat.py` | Connects the page to the chatbot: shared database, per-session bot, match-ID parsing, "open side by side". |
| `app/pages/6_Chatbot.py` | The chat page. |
| `tests/test_ui_home_chat.py` | 20 tests (home page, landing helpers, chat page). |

Aarav's `app/streamlit_app.py` is superseded by the chatbot page and can stay or go; the team can decide.

## 4. Running it

```bash
docker compose up -d                       # database
streamlit run app/Home.py
python -m pytest tests/test_ui_home_chat.py tests/test_ui_pages.py
```

For AI-written answers add the `SOCLAAS_*` settings to `.env`. Without them the chatbot answers from templates.

## 5. Verification and limits

Checked: all UI tests pass on the pipeline-plus-chatbot combination and on the manual-review branch; the home and chat pages were opened in a real browser in light and dark themes against a local PostgreSQL with the test data loaded; clicking a citation lands on the right match in Side by Side.

Not checked: AI-written answers and meaning-based search (no SoCLaaS key available when this was built), the Docker setup on a Mac, and Python 3.14. The chat page was tested with a stand-in bot for the answer layout and with the real bot in template mode.
