"""Run preprocessing, extraction, alignment, change analysis and materiality with PostgreSQL saves."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
from itertools import groupby
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from threading import RLock

from dotenv import dotenv_values

from sec_disclosure.llm.config import load_config
from sec_disclosure.llm.concurrency import run_jobs
from sec_disclosure.llm.disclosures import write_json
from sec_disclosure.llm.request_pacing import RequestPacer
from sec_disclosure.storage.disclosures import connection_arguments


ROOT = Path(__file__).resolve().parents[3]
PHASES = ("preprocessing", "extraction", "alignment", "change-analysis", "materiality")


@dataclass(frozen=True)
class Stage:
    name: str
    script: str
    arguments: tuple[str, ...]
    phase: str

    @property
    def command(self):
        return [sys.executable, str(ROOT / "scripts" / self.script), *self.arguments]

    @property
    def comparison(self):
        return tuple(self.arguments[self.arguments.index(flag) + 1]
                     for flag in ("--previous-year", "--current-year"))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AMD")
    parser.add_argument("--years", nargs="+", type=int, default=[2023, 2024, 2025])
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--user-agent", help="SEC contact; otherwise SEC_USER_AGENT from the environment/.env")
    parser.add_argument("--env-file", type=Path, help="Optional configuration file; otherwise repository .env and default SoCLaaS config")
    parser.add_argument("--workers", "--year-workers", dest="workers", type=int, default=3,
                        help="Concurrent disclosure-extraction year jobs (default: 3)")
    parser.add_argument("--batch-workers", type=int, default=6,
                        help="Concurrent extraction/consolidation batches within each year (default: 6)")
    parser.add_argument("--max-concurrent-requests", type=int,
                        help="Shared extraction API cap; defaults to active year workers times batch workers (18 for 3 years x 6)")
    parser.add_argument("--alignment-workers", type=int, default=9,
                        help="Concurrent alignment/change-analysis jobs per pair (default: 9)")
    parser.add_argument("--alignment-pair-workers", type=int, default=2,
                        help="Concurrent adjacent-year alignment/change-analysis pairs (default: 2)")
    parser.add_argument("--alignment-max-concurrent-requests", type=int,
                        help="Shared alignment/change-analysis API cap; defaults to active pairs times workers per pair (18 for 2 x 9)")
    parser.add_argument("--materiality-concurrency", "--materiality-workers", type=int, default=9,
                        help="Concurrent materiality batches per pair (default: 9)")
    parser.add_argument("--materiality-pair-workers", type=int, default=2,
                        help="Concurrent materiality year pairs (default: 2)")
    parser.add_argument("--materiality-max-concurrent-requests", type=int,
                        help="Shared materiality API cap; defaults to active pairs times batches per pair (18 for 2 x 9)")
    parser.add_argument("--embedding-model", default="bge-m3")
    parser.add_argument("--until", choices=PHASES, default="materiality",
                        help="Stop after this stage and its PostgreSQL saves (default: materiality, the full pipeline)")
    parser.add_argument("--from-stage", choices=PHASES, default="preprocessing",
                        help="Start at this stage using PostgreSQL inputs; earlier stages are skipped.")
    parser.add_argument("--dry-run", action="store_true", help="Print every command without downloads, model calls or database writes")
    args = parser.parse_args(argv)
    if PHASES.index(args.from_stage) > PHASES.index(args.until):
        parser.error("--from-stage must be at or before --until")
    args.ticker = args.ticker.lower()
    args.years = sorted(set(args.years))
    if args.max_concurrent_requests is None:
        args.max_concurrent_requests = min(args.workers, len(args.years)) * args.batch_workers
    if args.alignment_max_concurrent_requests is None:
        args.alignment_max_concurrent_requests = min(
            args.alignment_pair_workers, max(1, len(args.years) - 1)) * args.alignment_workers
    if args.materiality_max_concurrent_requests is None:
        args.materiality_max_concurrent_requests = min(
            args.materiality_pair_workers, max(1, len(args.years) - 1)) * args.materiality_concurrency
    args.data_dir = args.data_dir.expanduser().resolve()
    if args.env_file:
        args.env_file = args.env_file.expanduser().resolve()
        if not args.env_file.is_file():
            parser.error("--env-file does not exist")
    minimum_years = 2 if PHASES.index(args.until) >= PHASES.index("alignment") else 1
    if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", args.ticker) or len(args.years) < minimum_years
            or any(not 1900 <= year <= 2200 for year in args.years)
            or min(args.workers, args.batch_workers, args.max_concurrent_requests,
                   args.alignment_workers, args.alignment_pair_workers,
                   args.alignment_max_concurrent_requests, args.materiality_concurrency,
                   args.materiality_pair_workers, args.materiality_max_concurrent_requests) < 1):
        parser.error(f"Use a valid ticker, at least {minimum_years} fiscal year(s) (1900–2200), and positive workers.")
    # Parse literally; do not execute shell configuration or print credentials.
    for path in ([args.env_file] if args.env_file else []) + [ROOT / ".env"]:
        if path.is_file():
            for name, value in dotenv_values(path, interpolate=False).items():
                if value is not None:
                    os.environ.setdefault(name, value)
    args.user_agent = args.user_agent or os.environ.get("SEC_USER_AGENT", "")
    if args.from_stage == 'preprocessing' and not args.user_agent.strip():
        parser.error("Set SEC_USER_AGENT in .env or supply --user-agent with your SEC contact.")
    return args


def build_stages(args):
    stages = []
    data = args.data_dir
    disclosures = data / "disclosures"
    env = ["--env-file", str(args.env_file)] if args.env_file else []

    def add(phase, name, script, *arguments):
        stages.append(Stage(name, script, tuple(map(str, arguments)), phase))

    for year in args.years:
        add("preprocessing", f"text_{year}", "extract_filings.py", "--ticker", args.ticker, "--year", year,
            "--company", args.ticker, "--out-dir", data / "raw", "--user-agent", args.user_agent)
        # Sync only raw records. --replace deletes the filing and would cascade
        # into completed disclosures, so the full runner never uses that flag.
        add("preprocessing", f"save_text_{year}", "load_to_postgres.py", "--company", args.ticker,
            "--year", year, "--data-dir", data / "raw", "--sync")
    add("preprocessing", "api_tables_and_save", "run_api_metrics_batch.py", "--ticker", args.ticker,
        "--company", args.ticker, "--years", *args.years, "--merge",
        "--output-dir", data / "table_output" / args.ticker,
        "--sec-cache", data / "sec_cache", "--taxonomy-cache", data / "taxonomy_cache",
        "--user-agent", args.user_agent)
    add("extraction", "extract_disclosures_and_save", "extract_disclosures.py", "--ticker", args.ticker,
        "--years", *args.years, "--data-dir", data, "--output-root", disclosures,
        "--workers", args.workers, "--batch-workers", args.batch_workers,
        "--max-concurrent-requests", args.max_concurrent_requests, "--fault-tolerant", "--store-postgres",
        "--input-source", "postgres", *env)

    pairs = list(zip(args.years, args.years[1:]))
    for previous, current in pairs:
        output = data / "alignments" / args.ticker / f"{previous}-{current}"
        add("alignment", f"align_and_save_{previous}_{current}", "align_disclosures.py",
            "--ticker", args.ticker, "--previous-year", previous, "--current-year", current,
            "--data-dir", data, "--disclosures-dir", disclosures, "--output-dir", output,
            "--workers", args.alignment_workers, "--request-interval", "2",
            "--fault-tolerant", "--skip-change-analysis", "--store-postgres", "--input-source", "postgres", *env)
    for previous, current in pairs:
        output = data / "alignments" / args.ticker / f"{previous}-{current}"
        common = ["--ticker", args.ticker, "--previous-year", previous, "--current-year", current,
                  "--data-dir", data, "--disclosures-dir", disclosures]
        add("change-analysis", f"llm_changes_{previous}_{current}", "align_disclosures.py", *common,
            "--output-dir", output, "--workers", args.alignment_workers, "--request-interval", "2", "--fault-tolerant",
            "--change-analysis-only", "--store-postgres", "--input-source", "postgres", *env)
        add("change-analysis", f"lexical_semantic_{previous}_{current}", "run_aligned_change_analysis.py", *common,
            "--alignment-dir", output, "--embedding-model", args.embedding_model, "--input-source", "postgres", *env)
        add("change-analysis", f"save_changes_{previous}_{current}", "load_alignments_to_postgres.py",
            "--company", args.ticker, "--previous-year", previous, "--current-year", current,
            "--dir", output, *env)
    for previous, current in pairs:
        output = data / "alignments" / args.ticker / f"{previous}-{current}"
        add("materiality", f"materiality_{previous}_{current}", "classify_materiality.py", "--ticker", args.ticker,
            "--previous-year", previous, "--current-year", current, "--alignment-dir", output,
            "--concurrency", args.materiality_concurrency, "--request-interval", "2", "--input-source", "postgres", *env)
        add("materiality", f"save_materiality_{previous}_{current}", "load_alignments_to_postgres.py",
            "--company", args.ticker, "--previous-year", previous, "--current-year", current,
            "--dir", output, *env)
    return [stage for stage in stages if PHASES.index(args.from_stage) <= PHASES.index(stage.phase) <= PHASES.index(args.until)]


def preflight(args):
    target = PHASES.index(args.until)
    modules = ["lxml", "psycopg"]
    if target >= PHASES.index("extraction"):
        modules.extend(("openai", "langgraph"))
    if target >= PHASES.index("change-analysis"):
        modules.extend(("sklearn", "numpy"))
    for module in modules:
        if importlib.util.find_spec(module) is None:
            raise ValueError("Install requirements.txt into the selected Python environment first.")
    if target >= PHASES.index("extraction"):
        load_config(args.env_file)
    if args.from_stage == 'preprocessing' and not os.environ.get("SEC_API_KEY", "").strip():
        raise ValueError("Set SEC_API_KEY in .env before running API/XBRL table extraction.")
    import psycopg
    try:
        with psycopg.connect(**connection_arguments(args.env_file)) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                required = ["filings", "chunks", "sentences"]
                if args.from_stage != 'preprocessing':
                    required.append('pipeline_stage_snapshots')
                if target >= PHASES.index("extraction"):
                    required.append("disclosures")
                if target >= PHASES.index("alignment"):
                    required.extend(("alignment_runs", "alignments", "alignment_members"))
                cur.execute("SELECT name, to_regclass(name) FROM unnest(%s::text[]) AS name", (list(required),))
                missing = [name for name, relation in cur.fetchall() if relation is None]
                if missing:
                    raise ValueError("Missing PostgreSQL tables: " + ", ".join(missing) + ". Apply db/init schemas first.")
    except psycopg.Error:
        raise ValueError("Cannot connect to PostgreSQL. Start Docker/PostgreSQL and check POSTGRES_* / DATABASE_URL.") from None


def execute_stage(stage, *, request_pacer=None):
    if stage.script == "align_disclosures.py":
        # Both pairs use one limiter, including shared request spacing/cooldowns.
        from sec_disclosure.agents.disclosure_alignment import main as align
        return align(list(stage.arguments), request_pacer=request_pacer)
    if stage.script == "classify_materiality.py":
        from sec_disclosure.agents.materiality import main as classify
        return classify(list(stage.arguments), request_pacer=request_pacer)
    return subprocess.run(stage.command, cwd=ROOT, check=False).returncode


class StageStopped(RuntimeError):
    def __init__(self, name, code):
        self.name, self.code = name, code if code > 0 else 1
        super().__init__(name)


def main(argv=None):
    args = parse_args(argv)
    stages = build_stages(args)
    if args.dry_run:
        if any(stage.phase == "alignment" for stage in stages):
            print(f"Alignment/change analysis: up to {min(args.alignment_pair_workers, len(args.years) - 1)} "
                  f"parallel pairs x {args.alignment_workers} workers per pair; "
                  f"shared API cap {args.alignment_max_concurrent_requests}.")
        if any(stage.phase == "materiality" for stage in stages):
            print(f"Materiality: up to {min(args.materiality_pair_workers, len(args.years) - 1)} "
                  f"parallel pairs x {args.materiality_concurrency} workers per pair; "
                  f"shared API cap {args.materiality_max_concurrent_requests}.")
        for number, stage in enumerate(stages, 1):
            print(f"{number}/{len(stages)} {stage.name}\n  {shlex.join(stage.command)}")
        return 0
    try:
        preflight(args)
    except (ValueError, OSError) as error:
        print(f"Pipeline preflight failed: {error}", file=sys.stderr)
        return 1
    log_dir = args.data_dir / "pipeline_runs" / args.ticker / "-".join(map(str, args.years))
    log_dir.mkdir(parents=True, exist_ok=True)
    state = {"ticker": args.ticker, "years": args.years, "until": args.until,
             "from_stage": args.from_stage, "input_source": "postgres",
             "complete": False, "full_pipeline_complete": False,
             "extraction_concurrency": {"year_workers": min(args.workers, len(args.years)),
                                        "batch_workers_per_year": args.batch_workers,
                                        "max_concurrent_requests": args.max_concurrent_requests},
             "alignment_concurrency": {"pair_workers": min(args.alignment_pair_workers, len(args.years) - 1),
                                       "workers_per_pair": args.alignment_workers,
                                       "max_concurrent_requests": args.alignment_max_concurrent_requests},
             "materiality_concurrency": {"pair_workers": min(args.materiality_pair_workers, len(args.years) - 1),
                                         "workers_per_pair": args.materiality_concurrency,
                                         "max_concurrent_requests": args.materiality_max_concurrent_requests},
             "started_at": datetime.now(timezone.utc).isoformat(), "stages": []}
    state_lock = RLock()
    numbers = {stage.name: number for number, stage in enumerate(stages, 1)}

    def run_one(stage, pacer):
        entry = {"stage": stage.name, "phase": stage.phase, "status": "running"}
        with state_lock:
            print(f"\n[{numbers[stage.name]}/{len(stages)}] {stage.name}", flush=True)
            state["stages"].append(entry)
            write_json(log_dir / "status.json", state)
        try:
            code = execute_stage(stage, request_pacer=pacer)
        except KeyboardInterrupt:
            code = 130
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 1
        except Exception as error:
            print(f"{stage.name} failed ({type(error).__name__}); saved work is retained.", file=sys.stderr)
            code = 1
        with state_lock:
            entry.update(status="complete" if code == 0 else "stopped", exit_code=code)
            write_json(log_dir / "status.json", state)
        if code:
            raise StageStopped(stage.name, code)

    def run_chain(chain, pacer):
        for stage in chain:
            run_one(stage, pacer)

    try:
        for phase, members in groupby(stages, key=lambda stage: stage.phase):
            phase_stages = list(members)
            if phase in {"alignment", "change-analysis", "materiality"}:
                chains = {}
                for stage in phase_stages:
                    chains.setdefault(stage.comparison, []).append(stage)
                pair_workers, workers, api_cap = (
                    (args.materiality_pair_workers, args.materiality_concurrency, args.materiality_max_concurrent_requests)
                    if phase == "materiality" else
                    (args.alignment_pair_workers, args.alignment_workers, args.alignment_max_concurrent_requests))
                pacer = RequestPacer(2.0, 60.0, api_cap)
                print(f"\n{phase}: {min(pair_workers, len(chains))} concurrent pairs x "
                      f"{workers} workers per pair; shared API cap {api_cap}.", flush=True)
                # Each comparison finishes its ordered saves; all admitted pairs
                # drain before a failure can stop the next pipeline phase.
                run_jobs(list(chains.values()), lambda chain: run_chain(chain, pacer),
                         lambda chain, result: None, pair_workers)
            else:
                run_chain(phase_stages, None)
    except StageStopped as error:
        print(f"Pipeline stopped at {error.name} (exit {error.code}). "
              "Saved work is retained. Rerun the same command to resume using stage caches.", file=sys.stderr)
        return error.code
    state.update(complete=True, full_pipeline_complete=args.until == "materiality",
                 completed_at=datetime.now(timezone.utc).isoformat())
    write_json(log_dir / "status.json", state)
    print(f"\nPipeline completed through {args.until} for {args.ticker.upper()}: "
          "every completed stage saved to PostgreSQL.", flush=True)
    return 0
