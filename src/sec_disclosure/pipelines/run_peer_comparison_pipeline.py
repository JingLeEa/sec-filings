"""Run a separate peer comparison using existing PostgreSQL company data."""

from __future__ import annotations

import sys
from copy import copy

from sec_disclosure.agents import peer_comparison
from sec_disclosure.pipelines.pipeline_progress import invocation_lock
from sec_disclosure.storage.peer_comparison import read_peer_inputs, store_peer_report
from sec_disclosure.storage.artifacts import PostgresWorkspace


def main(argv=None) -> int:
    args = peer_comparison.parse_args(argv)
    try:
        # Check all requested tickers before output directories,
        # cached jobs, model requests or database report writes are touched.
        inputs = read_peer_inputs(args.ticker, args.peers, int(args.year), env_file=args.env_file)
        if args.dry_run:
            peer_comparison.run(args, database_inputs=inputs)
            return 0
        with PostgresWorkspace('peer', args.env_file) as workspace:
            runtime = copy(args)
            runtime.output_dir = workspace.root / 'peer_comparison' / args.ticker / args.year / '-'.join(args.peers)
            with invocation_lock(runtime.output_dir):
                workspace.import_local(args.output_dir, destination=runtime.output_dir,
                                       marker='peer-' + args.ticker + '-' + args.year + '-' + '-'.join(args.peers))
                report = peer_comparison.run(runtime, database_inputs=inputs)
                report.update(input_source="postgres", workers=args.workers, top_k=args.top_k)
                store_peer_report(report, env_file=args.env_file)
                if args.save_local_output:
                    workspace.export(args.output_dir, source=runtime._cache_output)
                print("Peer comparison and token usage saved to PostgreSQL peer_comparison_reports.")
                return 0 if report["run_complete"] else 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(f"Peer comparison pipeline failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
