# Windows setup

Use PowerShell from the repository root. Install Python 3.13 and Docker Desktop,
start Docker Desktop, and select Linux containers. A new PowerShell window may be
needed after installing Python so its launcher is on PATH.

Create the project environment and install the backend, dashboard, and test tools:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r app\requirements.txt -e ".[test]"
docker compose up -d --wait
```

These commands use the environment's Python directly, so activating a script or
changing PowerShell's execution policy is unnecessary.

If `.env` does not exist, copy `.env.example` to `.env`. Keep existing settings
when configuring a machine that has already been used for this project. Fill in
your own `SEC_USER_AGENT` contact, sec-api.io `SEC_API_KEY`, and `SOCLAAS_API_KEY`;
use the SoCLaaS endpoint and model supplied for your account. The database uses
the local defaults in the template. `.env` is ignored by Git.

Open the dashboard:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app\Home.py
```

Startup imports missing bundled reports into PostgreSQL automatically. To prepare
those reports without launching the dashboard:

```powershell
.\.venv\Scripts\python.exe scripts\seed_starter_data.py
```

Run preprocessing through synthesis:

```powershell
.\.venv\Scripts\python.exe scripts\run_full_pipeline.py --ticker AMD --years 2023 2024 2025 --env-file .env
```

The default pipeline stores result artifacts, request caches, and checkpoints in
PostgreSQL. Add `--save-local-output` to export results. Resume uses the same
database and cache namespace. Source fingerprints ignore LF/CRLF checkout
differences; actual source, evidence, model, or configuration changes still
undergo the existing cache validation.

Completed starter selections are already marked ready. Their normal full-pipeline
command returns the existing results without making new API requests. Use
`--no-resume` only when you deliberately want to recompute a starter selection.

Peer comparison requires stored disclosures for the target and every peer.
The default target selection also requires completed materiality results:

```powershell
.\.venv\Scripts\python.exe scripts\run_peer_comparison_pipeline.py --ticker AMD --peers NVDA --year 2024 --env-file .env --dry-run
.\.venv\Scripts\python.exe scripts\run_peer_comparison_pipeline.py --ticker AMD --peers NVDA --year 2024 --env-file .env
```

The preview reads PostgreSQL without model calls or report writes. An uncached
full pipeline or peer comparison consumes API quota.

Run offline tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

For the PostgreSQL peer tests, which create and remove isolated test databases:

```powershell
$env:SEC_PEER_DB_INTEGRATION = "1"
.\.venv\Scripts\python.exe -m pytest -q tests\test_peer_comparison_postgres.py
Remove-Item Env:SEC_PEER_DB_INTEGRATION
```

PowerShell uses `$env:NAME = "value"` for environment variables. The Unix
`export NAME=value`, `.venv/bin/python`, and backslash line-continuation examples
in other guides must be translated; the commands above are single lines.
