# F2F — SEC Disclosure Comparison

Compare companies' SEC 10-K filings across fiscal years, read synthesis reports, ask questions with cited evidence, and review the AI's labels. Peer comparison is available as a separate workflow.

This guide takes you from a fresh download to using the dashboard on **macOS or Windows**. You can explore bundled reports before adding API keys.

## 1. Install the prerequisites

Install these once:

| Tool | What to install |
| --- | --- |
| Python | [Python 3.13](https://www.python.org/downloads/) for this project's setup commands. On Windows, enable the Python launcher and add Python to PATH when offered. |
| Docker Desktop | Install for [Mac](https://docs.docker.com/desktop/setup/install/mac-install/) or [Windows](https://docs.docker.com/desktop/setup/install/windows-install/), then open it and finish its setup. On Windows, use Linux containers. |
| Git | [Install Git](https://git-scm.com/downloads/) if you want to clone and pull updates. You can also download the project as a ZIP. |

Keep Docker Desktop running while using the app. PostgreSQL runs on your computer through Docker; a hosted database is not required for this setup. Internet access is needed for installation and new filing or AI requests.

## 2. Download the project

Open **Terminal** on Mac or **PowerShell** on Windows, then run:

```sh
git clone https://github.com/JingLeEa/sec-filings.git
cd sec-filings
```

Alternatively, open the [GitHub repository](https://github.com/JingLeEa/sec-filings), choose **Code → Download ZIP**, and extract it. Open a terminal in the extracted folder. For example:

```sh
cd "path/to/sec-filings-main"
```

Run all remaining commands from the project folder containing `README.md`, `app/`, and `docker-compose.yml`.

## 3. Install the app

For shared Google Cloud API access, ask the maintainer to grant your Google
account **Secret Manager Secret Accessor** on `bt4103-api-key`, then run one
setup command from the project folder:

**macOS:**

```sh
python3.13 scripts/setup_app.py
```

**Windows PowerShell:**

```powershell
py -3.13 scripts\setup_app.py
```

Setup creates or reuses `.venv`, installs the app's Python dependencies, and
downloads Google's CLI into the ignored `.tools` folder if needed. Downloads
use a pinned release with Google's published SHA256 checksum. Existing local
settings are preserved; a fresh installation gets a `.env` containing only the
local PostgreSQL settings. No administrator rights, Homebrew, or PowerShell
execution-policy changes are needed.

If your existing Google credentials can access the shared secret, setup reuses
them. Otherwise, complete the Google sign-in in your browser using the account
granted access. Setup checks that the shared API settings are present; it does
not run a filing pipeline or call SEC/AI providers. If a step fails, fix the
reported issue and rerun the same setup command.

After **Setup complete**, go to **step 5** to start PostgreSQL, then launch with:

```sh
# macOS
.venv/bin/python scripts/run_with_gcp_secrets.py
```

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe scripts\run_with_gcp_secrets.py
```

Keep Docker Desktop running while using the dashboard. Google credentials are
stored in Google's normal local credential location; fetched API keys stay in
the app's memory. Reuse the launch command for later sessions. To rerun setup
without a browser prompt, add `--skip-login`; to reuse already installed Python
packages, also add `--skip-install`.

### Manual Python installation

If you prefer to install dependencies manually and use your own local API
settings, use the commands below and continue with step 4. The `.venv` folder
keeps this project's Python dependencies together; you do not need to activate it.

#### macOS — Terminal

```sh
python3.13 --version
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r app/requirements.txt
```

#### Windows — PowerShell

```powershell
py -3.13 --version
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r app\requirements.txt
```

Wait for installation to finish without errors. Create a separate `.venv` on each computer; do not copy one between Mac and Windows.

## 4. Create your settings file

Create `.env` in the project folder using the appropriate command below. These commands preserve an existing `.env`.

**macOS:**

```sh
test -f .env || cp .env.example .env
```

**Windows PowerShell:**

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Open `.env` in a text editor. On Windows, ensure it is named `.env`, not `.env.txt`. On Mac, press **Command + Shift + .** in Finder to show hidden files.

For a new local installation, the database settings can remain:

```dotenv
POSTGRES_HOST="127.0.0.1"
POSTGRES_PORT="5432"
POSTGRES_USER="sec"
POSTGRES_PASSWORD="sec_local_pw"
POSTGRES_DB="sec_disclosure"
```

Fill in the remaining settings according to what you want to do:

| Setting | Needed for | What to enter |
| --- | --- | --- |
| `SEC_USER_AGENT` | Running a new company's pipeline | Your name and contact email, such as `Your Name your.email@example.com`. |
| `SEC_API_KEY` | Financial table/XBRL extraction in a new pipeline run | Your **sec-api.io** API key. |
| `SOCLAAS_BASE_URL` | AI pipeline stages, peer comparisons, and AI-written chatbot answers | The HTTPS endpoint supplied for your SoCLaaS account. The template includes the project's endpoint. |
| `SOCLAAS_API_KEY` | The same AI features | Your SoCLaaS API key. This is a different key from `SEC_API_KEY`. |
| `SOCLAAS_MODEL` | The same AI features | The model name supplied for your account; keep `default` only if your account supports it. |

**To browse starter reports, leave the API keys blank.** The chatbot can also give template answers from saved database results without an AI key. To run a new company, complete all the settings above first.

Save the file before launching the app. After changing settings, restart the app; changes do not update a pipeline that is already running. Keep `.env` private and out of GitHub; it is ignored by Git. Each new installation needs its own settings file.

### Use shared API settings from Google Cloud

You can load the API settings from **Google Cloud Secret Manager** instead of
putting API keys in each person's `.env`. Keep the local PostgreSQL settings
above in `.env`; the database still runs through Docker on each person's computer.
The normal dependency installation in step 3 includes the Google Python client.

The committed [gcp-secrets.json](gcp-secrets.json) contains only these identifiers:

```json
{
  "project": "bt4103-514213",
  "secret": "bt4103-api-key",
  "version": "latest"
}
```

The secret's value must be the **contents of the combined `.env` file**, including
`SEC_USER_AGENT`, `SEC_API_KEY`, `SOCLAAS_BASE_URL`, `SOCLAAS_API_KEY`, and
`SOCLAAS_MODEL`. Upload `gcp-secrets.env` if the maintainer has prepared it locally.
That private file is ignored by Git and is not included when someone clones the
project. `SOCLAAS_CHATBOT_MODEL` and the `DATABASE_URL`/`POSTGRES_*` settings from
`.env.example` are also supported, but local database settings normally stay local.

Use the **one-command setup in step 3** on each computer. It handles both Google
CLI installation and Google application-default sign-in. Cloning the repository
does not grant access to the secret; the maintainer must grant your Google
account Secret Accessor. This role lets you retrieve the actual shared API keys.
No service-account key JSON is needed. If you prefer a system-wide CLI, install
it using [Google's installer](https://docs.cloud.google.com/sdk/docs/install-sdk)
and sign in with `gcloud auth application-default login --project=bt4103-514213`.

**Check access without starting the app or calling SEC/AI providers:**

```sh
# macOS
.venv/bin/python scripts/run_with_gcp_secrets.py --check
```

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe scripts\run_with_gcp_secrets.py --check
```

**Launch the dashboard with the shared API settings**, after starting Docker as
described in step 5:

```sh
# macOS
.venv/bin/python scripts/run_with_gcp_secrets.py
```

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe scripts\run_with_gcp_secrets.py
```

This is an alternative to the local-file launch commands in step 6. The launcher
fetches and validates the selected secret version once, then starts Streamlit
using the same Python environment. The settings stay in process memory and take
priority over local API settings. Pipeline jobs, peer comparison, chatbot
requests, and Manual Review background jobs inherit them. The launcher clears
the old `SEC_UI_PIPELINE_ENV_FILE` override because review workers do not need a
downloaded env file. It never writes the fetched settings to a file, PostgreSQL,
or an activity log, and it does not start the app if secret access fails.

For a terminal pipeline, put the Python script and its arguments after `--`:

```sh
.venv/bin/python scripts/run_with_gcp_secrets.py -- scripts/run_full_pipeline.py --ticker MS --years 2023 2024 2025 --until preprocessing
```

On Windows, use `.\.venv\Scripts\python.exe` in place of `.venv/bin/python`.
The same launcher can run `scripts/run_peer_comparison_pipeline.py` or other
Python entry points. Use `--project`, `--secret`, or `--version` before `--` to
override the committed identifiers.

**Maintainer: upload an updated file** as a new version, then restart the app:

```sh
.venv/bin/python -S .tools/google-cloud-sdk/lib/gcloud.py secrets versions add bt4103-api-key --project=bt4103-514213 --data-file=gcp-secrets.env
```

For this upload command, authenticate the CLI with the same prefix followed by
`auth login` if needed, for example
`.venv/bin/python -S .tools/google-cloud-sdk/lib/gcloud.py auth login`.
Google CLI login and application-default login use separate credentials. If you
use a system-wide CLI, replace `.venv/bin/python -S .tools/google-cloud-sdk/lib/gcloud.py`
with `gcloud`. On Windows, use `.\.venv\Scripts\python.exe` for the Python path.

Existing runs keep the version loaded when they started. New launches use
`latest`; you can select a fixed version with `--version 2`.

If access fails, check that the Secret Manager API is enabled in this project,
that the secret contains the complete `.env` text, and that your account has
Secret Accessor on this secret. If Google reports a quota-project permission
error, ask the maintainer for **Service Usage Consumer** on the quota project,
then run `gcloud auth application-default set-quota-project bt4103-514213`.
See [Google's authentication guide](https://docs.cloud.google.com/secret-manager/docs/authentication)
and [quota-project instructions](https://docs.cloud.google.com/sdk/gcloud/reference/auth/application-default/set-quota-project).

## 5. Start PostgreSQL

With Docker Desktop running, execute:

```sh
docker compose up -d --wait
docker compose ps
```

The `db` service should show as **healthy**. The first start downloads the database image and can take longer. Resolve any database startup error before continuing.

## 6. Open the dashboard

The launch commands below also tell Manual Review to use your project's `.env` when refreshing affected reports.

**macOS:**

```sh
SEC_UI_PIPELINE_ENV_FILE="$PWD/.env" .venv/bin/python -m streamlit run app/Home.py
```

**Windows PowerShell:**

```powershell
$env:SEC_UI_PIPELINE_ENV_FILE = (Join-Path (Get-Location) ".env")
.\.venv\Scripts\python.exe -m streamlit run app\Home.py
```

Open the **Local URL** printed in the terminal, usually **http://localhost:8501**. If Streamlit selects another port, use the printed URL. Keep the terminal open and Docker Desktop running.

On the first launch, bundled reports are loaded into PostgreSQL automatically. Wait for the Home page to finish loading. Later launches keep existing results and human review decisions.

### Data available immediately

| Sector | Bundled companies |
| --- | --- |
| Semiconductors | AMD, AVGO (Broadcom), INTC (Intel), MU (Micron), NVDA (NVIDIA), QCOM (Qualcomm) |
| Banking | BAC (Bank of America), C (Citigroup), GS (Goldman Sachs), JPM (JPMorgan Chase), MS (Morgan Stanley) |

Available years and report coverage vary by company. Use the dashboard's selectors to see the saved choices. These are shipped examples; cloning the repository does not copy another person's private PostgreSQL database or their later runs.

### Load an updated starter pack

After pulling an updated `main` branch, start Docker Desktop and run the command
below from the project folder. It loads missing data and refreshes older,
untouched starter records in PostgreSQL. No SEC or AI requests are made.

**macOS:**

```sh
docker compose up -d --wait
.venv/bin/python scripts/seed_starter_data.py --refresh
```

**Windows PowerShell:**

```powershell
docker compose up -d --wait
.\.venv\Scripts\python.exe scripts\seed_starter_data.py --refresh
```

The command reports what was refreshed, unchanged, or protected. Companies with
human reviews, pending review updates, active/unfinished pipelines, or your own
results are preserved. Unrecognised or edited records are also protected. Without
`--refresh`, the command only loads missing scopes, like normal app startup.
Click **Reload data** in the dashboard, or restart Streamlit, after it finishes.
This loads the starter pack included in
your checkout; fetching `test-data` and rebuilding the pack is a separate
[maintainer step](starter_data/README.md).

### Try your first report

1. On **Home**, choose **AMD** and **FY2024 to FY2025** in the sidebar.
2. Review the snapshot, then open **Side by Side** to inspect a change and its filing evidence.
3. Open **Synthesis Report** and select the saved AMD report under **Synthesis output**.
4. Open **Chatbot** and ask: `What are the most material changes in AMD's 2025 10-K?`
5. Open **Peer Comparison → Saved comparisons** to explore a stored comparison without running a new one.

## 7. Use the dashboard

The sidebar menu is ordered: **Home → Run Pipeline → Synthesis Report → Chatbot → Side by Side → Manual Review → Change Explorer → Peer Comparison**.

### Home

Choose a **Company** and **Filing years** in the sidebar to see matched groups, changed groups, material changes, and records needing review. Use the cards and snapshot buttons to open the corresponding page. **Reload data** refreshes the displayed results after a pipeline run or manual review.

### Run Pipeline

Use this page to prepare a company and its fiscal years through synthesis.

1. Open **Run setup & progress**.
2. Enter a **Company ticker** and at least two comma-separated **Fiscal years**, such as `2023, 2024, 2025`. On a fresh installation, `TXN` (Texas Instruments) is an example outside the bundled companies.
3. Check the comparison pairs shown below the inputs. With three selected years, the app compares the first with the second, then the second with the third. Years do not have to be consecutive.
4. If selecting more than two years, confirm **Continue with these fiscal years**.
5. Select **Start pipeline**. New runs use your API accounts and consume their quota.

The dashboard tracks six stages: **Preprocessing, Disclosure extraction, Alignment, Change analysis, Materiality, and Synthesis**. The default run finishes at synthesis. Peer comparison is started separately.

While a run is active, the setup fields show its ticker and years and are disabled. Progress and the activity log show the work completed and any errors. After completion, open **Synthesis Report** to read the saved report.

If the selected company and years already have completed results, the app tells you and directs you to those results. The form normally starts with the most recent run selection; you can edit it when no run is active. Completed starter selections are also recognised as ready.

#### Stop, resume, and failures

- Select **Stop pipeline** to stop the current run. Saved work remains available, and you can then enter another company or different years.
- Return to an unfinished selection and select **Resume pipeline** to continue from reusable saved work.
- Eligible failed stages receive up to **five automatic retries after the first attempt**. If recovery fails, read the error in the activity log, fix the cause, and resume. User-requested stops are not automatically retried.
- If the app offers **Retry failed AI requests on resume**, enable it when you want to permit new attempts for failed requests. Successful reusable responses and completed stages are retained.

#### Run history

Open the **Run history** tab to see previously started runs, newest first, with company, fiscal years, status, timestamps, and last stage. Times are displayed in **Singapore time (SGT)**.

Filter by **Company**, **Status**, or **Contains fiscal years**, then choose a **Saved run** to inspect its details and activity log. For an unfinished or failed run, use **Resume pipeline** directly in this tab. Resume is unavailable while another run is active or when that selection has since completed.

**Load selection in Run setup** fills the form without starting work. **Download run history CSV** exports the history. Each new start or resume attempt gets its own entry; bundled reports do not invent historical execution entries.

### Synthesis Report

Choose a saved **Synthesis output** in the sidebar. Read the executive summary, key findings, changes over time, and financial analysis where available. Follow evidence links to check the underlying comparisons, and review coverage and limitation notes alongside the conclusions.

Use **Download** to save the Markdown report, the evidence table as CSV, or the full output as JSON. A new report appears after the main pipeline completes synthesis.

### Chatbot

Ask about saved companies, disclosure changes, financial figures, or peer comparisons. Include the company and fiscal year in your first question; follow-up questions can use the conversation's context.

For example:

- `What changed in NVIDIA's risk factors between 2024 and 2025?`
- `Show AMD's material changes in 2025.`
- `Which evidence supports that finding?`

Keep **AI-written answers** enabled to use your SoCLaaS account, or turn it off for template answers from the same database search. Without an AI key, template answers are used automatically. **Meaning-based search** needs an AI key and embedded evidence; keyword search is used when these are unavailable.

Open citations to inspect supporting evidence side by side. Use **New conversation** to start fresh and **Past conversations** to reopen saved chats.

### Side by Side

Choose a **Company** and **Filing years**, apply filters, and select a match. Read the earlier and later disclosure text, the change label, and the explanation. With highlighting enabled, added text is green and removed text is red. Source links open the SEC filing.

A missing counterpart means the system has no matching disclosure on that side; check the evidence before interpreting it as the company's silence on a topic.

### Manual Review

Use the combined **Review queue** block to choose both the work and its filters:

1. Under **What to review**, choose **Disclosure** to review a disclosure's category, or **Alignment** to review the change label between filing years.
2. Under **Which records**, choose **Needs review** or **AI approved**.
3. Choose the company and year or year pair in the sidebar. Narrow the queue using **SEC Item**, **Review status**, and **Search text** in the same block. Use **Clear filters** to reset them.
4. Select a record and read its evidence and reason for review. For alignment, compare both filing years.
5. Choose **Human disclosure type** or **Human change label**, add optional review notes, then select **Save review** or **Save and next**.

The page distinguishes the original AI label from the saved human decision. Human labels and notes persist in PostgreSQL across app restarts. Saving also queues affected downstream results for refresh; monitor **Pipeline updates** on this page. If it fails, your review remains saved; use **Retry pipeline refresh** after fixing the reported cause.

For a complete review-and-refresh workflow, use a company/year selection you processed through the full pipeline. Starter reports support browsing and saved labels but do not contain all original preprocessing inputs needed to regenerate downstream reports. AI refreshes also require valid API settings.

### Change Explorer

Choose a company and filing-year pair, then filter the results by section, category, change label, materiality, or text. Select a row to inspect it and use **Open side by side** to read both years together. The **Export** section offers CSV and JSON downloads of the filtered results.

### Peer Comparison

Peer comparison uses existing PostgreSQL inputs. **The target and every peer must already have saved filings and extracted disclosures for the same fiscal year.** Run the main pipeline for missing companies before starting a comparison.

1. Enter the **Target ticker**, **Peer tickers** separated by commas or spaces, and **Fiscal year**. For example: target `AMD`, peers `NVDA, INTC`, year `2024`.
2. Choose **Items to compare**.
3. Choose **Target disclosures**: **Material changes** requires the target's saved materiality results for that year and an adjacent year. **All disclosures** uses saved disclosures without requiring materiality results.
4. Select **Check prerequisites**. This checks saved inputs without making AI requests. Resolve any missing-input message first.
5. Select **Start peer comparison**. The main pipeline must finish before you can start it.
6. Monitor progress and the **Peer comparison activity log**. If a comparison stops or is incomplete, check prerequisites and use **Resume peer comparison** for the same selection.

Use **Saved comparisons** to open a report, filter its results, read cited sentences from both companies, and download Markdown, CSV, or JSON. Results and reusable comparison caches are saved in PostgreSQL. A missing peer match is not proof that the peer omitted the topic.

## 8. Where your work is saved

| Work | Default location |
| --- | --- |
| Pipeline results, request caches, and resumable checkpoints | PostgreSQL |
| Synthesis and peer comparison reports | PostgreSQL |
| Human review labels, notes, and review refresh jobs/logs | PostgreSQL |
| Run history and saved chatbot conversations | PostgreSQL |
| Files you explicitly download from the UI | Your browser's download location |

PostgreSQL itself uses a Docker volume on your computer, so the database survives normal app and container restarts. This setup does not share new runs automatically with groupmates on other computers.

The dashboard's pipeline launchers also keep local process metadata and activity logs under `data/`. Generated pipeline results and caches stay in PostgreSQL by default. Optional command-line local exports are covered in the [pipeline guide](docs/ZIYANG_full_pipeline.md).

## 9. Close, reopen, and update

### Closing the app

Closing a browser tab does not stop a pipeline. To stop work, select **Stop pipeline** in the dashboard first. Press **Ctrl+C** in the Streamlit terminal to close the web server; a separately launched pipeline may continue, so use the dashboard's stop control when you want to stop it.

When no work is running, you can stop the database with:

```sh
docker compose down
```

This retains its data. **Do not add `-v` unless you intend to delete the database volume and its saved work.**

A sleeping or powered-off computer cannot keep a local pipeline processing. Keep it awake during a run. Saved checkpoints allow you to resume unfinished work afterwards.

### Opening it next time

1. Open Docker Desktop.
2. Open a terminal in the project folder.
3. Run `docker compose up -d --wait`.
4. Use your operating system's launch command from [Open the dashboard](#6-open-the-dashboard).
5. Check **Run Pipeline → Run history**. An active worker can reconnect to the UI; if an unfinished run has stopped, select **Resume pipeline**.

You do not need to recreate `.venv`, copy `.env` again, or reinstall packages for each launch.

### Updating your installation

Stop active work and close the app before updating. If you cloned with Git and have no local changes to preserve, run:

```sh
git pull
```

Then reinstall dependencies using the existing environment:

**macOS:**

```sh
.venv/bin/python -m pip install -r app/requirements.txt
```

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m pip install -r app\requirements.txt
```

Launch the app again. Keep your existing `.env` and Docker database volume.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| `python3.13` or `py -3.13` is not found | Install Python 3.13, then open a new terminal. On Windows, ensure the Python launcher was installed. |
| `docker` is not found or cannot connect to the Docker daemon | Install/open Docker Desktop, finish its setup, and wait for it to start. Reopen your terminal if necessary. |
| PostgreSQL cannot connect or starter data fails to load | Run `docker compose ps` and `docker compose logs db`. Check that `db` is healthy and `.env` matches the database settings. Reload the app after correcting the issue. |
| Port `5432` is already in use | Set `POSTGRES_PORT="5433"` in `.env`, run `docker compose up -d --wait`, and restart the app. |
| `ModuleNotFoundError`, including `psutil` | Stop Streamlit and rerun your OS's dependency-install command above. Launch with the same `.venv` Python, rather than a bare `streamlit` or `pip` command. |
| Browser cannot open the app | Check that the Streamlit terminal is still running and open its printed **Local URL**. It may use a port other than `8501`. |
| Missing API settings or authentication failure | Check the corresponding key, endpoint, and model in `.env`. A sec-api.io key and a SoCLaaS key are separate. Restart the app after edits. |
| Pipeline stops after retries | Read the activity log, fix the reported issue, then resume from **Run setup & progress** or **Run history**. Saved successful work can be reused. |
| Peer comparison cannot start | Select **Check prerequisites** and prepare the named missing company/year inputs in **Run Pipeline**. Wait for an active main pipeline to finish. |
| Review is saved but report refresh fails | Check **Manual Review → Pipeline updates**. Confirm the launch command includes the settings-file variable, API settings are valid, and full pipeline inputs exist; then retry the refresh. |
| Counts or reports look stale | Select **Reload data** in the sidebar, or reload the page after the run/refresh finishes. |

## Further help

- [Bundled starter data](starter_data/README.md) — example data coverage and limitations.
- [Windows setup](docs/windows_setup.md) — additional PowerShell commands.
- [Full pipeline guide](docs/ZIYANG_full_pipeline.md) — optional command-line controls and local exports.
- [Peer comparison guide](docs/ARIEL_ZIYANG_peer_company_comparison_agent.md) — additional comparison options.
- [Chatbot guide](docs/AARAV_chatbot.md) — question examples and configuration.
