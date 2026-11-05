# macOS and Windows

The pipeline and dashboard use the same Python source and dependencies on both
systems. The platform CI matrix tests Python 3.13 and 3.14 on native macOS and
Windows runners. A separate Linux job exercises PostgreSQL-backed preprocessing,
extraction, alignment, change analysis, materiality, synthesis, peer comparison,
and review refresh with offline API fixtures.

CI runs after the workflow is pushed to GitHub, on pull requests, or through
manual dispatch. Configuring the matrix does not itself constitute a passing
macOS run. Linux tests exercise the shared POSIX paths, but a native macOS CI
result is still required to confirm the Mac environment.

Use a separate virtual environment on each machine. Do not transfer `.venv`
between macOS and Windows. The launcher selects `.venv/bin/python` on macOS and
`.venv/Scripts/python.exe` on Windows, falling back to the active interpreter
when the native project environment is absent.

| Operation | macOS terminal | Windows PowerShell |
| --- | --- | --- |
| Create environment | `python3 -m venv .venv` | `py -3.13 -m venv .venv` |
| Install dependencies | `.venv/bin/python -m pip install -r requirements.txt -r app/requirements.txt -e ".[test]"` | `.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r app\requirements.txt -e ".[test]"` |
| Start PostgreSQL | `docker compose up -d --wait` | `docker compose up -d --wait` |
| Run tests | `.venv/bin/python -m pytest -q` | `.\.venv\Scripts\python.exe -m pytest -q` |
| Open dashboard | `.venv/bin/python -m streamlit run app/Home.py` | `.\.venv\Scripts\python.exe -m streamlit run app\Home.py` |

The OS-specific behavior is limited to file locks and process launch flags.
macOS retains `fcntl.flock` locks and a separate process session/group; Windows
uses `msvcrt` locks and Windows process creation flags. Both systems use safe PID
checks, UTF-8 text, forward-slash database keys, and source fingerprints that
ignore Git checkout line endings.

First-time snapshot schema creation is synchronized in PostgreSQL so parallel
year saves cannot conflict while initializing a fresh database.

The PostgreSQL configuration and `.env` variable names are shared. Fill in your
own SEC contact and provider keys for fresh live runs. See
[Windows setup](windows_setup.md) and the [full pipeline guide](ZIYANG_full_pipeline.md)
for command details. CI does not require those provider keys or call paid APIs.
