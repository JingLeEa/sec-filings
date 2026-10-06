"""Batch defaults, resumability, failure isolation and unchanged yearly outputs."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import run_api_metrics_batch as batch
import merge_api_metrics


def run(args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = batch.main(args)
    return code, stdout.getvalue() + stderr.getvalue()


class BatchPipelineTests(unittest.TestCase):
    def test_ticker_alone_uses_three_default_years_and_shared_company_folder(self):
        with patch.object(batch.pipeline, 'main', return_value=0) as single, \
                patch.object(Path, 'is_file', return_value=False):
            code, log = run(['--ticker', 'intc'])
        self.assertEqual(code, 0, log)
        commands = [call.args[0] for call in single.call_args_list]
        self.assertEqual([args[args.index('--year') + 1] for args in commands], ['2023', '2024', '2025'])
        for args in commands:
            self.assertEqual(args[args.index('--ticker') + 1], 'INTC')
            self.assertEqual(args[args.index('--output-dir') + 1], 'tests/for_table_development/intc')
            self.assertEqual(args[args.index('--items') + 1:args.index('--year')], ['1', '1A', '7', '8'])

    def test_options_forwarded_and_duplicate_years_run_once(self):
        with TemporaryDirectory() as tmp, patch.object(batch.pipeline, 'main', return_value=0) as single:
            code, log = run(['--cik', '50863', '--company', 'Intel', '--years', '2025', '2023', '2025',
                             '--items', '7', '8', '--output-dir', tmp, '--sec-cache', 'some/cache',
                             '--taxonomy-cache', 'some/taxonomy', '--user-agent', 'Test test@example.com'])
        self.assertEqual(code, 0, log)
        commands = [call.args[0] for call in single.call_args_list]
        self.assertEqual([a[a.index('--year') + 1] for a in commands], ['2023', '2025'])
        for args in commands:
            for flag, value in [('--cik', '50863'), ('--company', 'Intel'), ('--output-dir', tmp),
                                ('--sec-cache', 'some/cache'), ('--taxonomy-cache', 'some/taxonomy'),
                                ('--user-agent', 'Test test@example.com')]:
                self.assertEqual(args[args.index(flag) + 1], value)
            self.assertEqual(args[args.index('--items') + 1:args.index('--user-agent')], ['7', '8'])

    def test_year_failure_does_not_prevent_later_years_and_exit_is_nonzero(self):
        for failure in (1, ValueError('Unverified financial statements')):
            with self.subTest(failure=failure), TemporaryDirectory() as tmp, \
                    patch.object(batch.pipeline, 'main', side_effect=[0, failure, 0]) as single:
                code, log = run(['--ticker', 'INTC', '--output-dir', tmp])
            self.assertEqual(code, 1)
            self.assertEqual(single.call_count, 3)
            self.assertIn('FY2024: failed', log)
            self.assertIn('FY2025: created', log)
            self.assertIn('2 created; 0 existing files kept; 1 failed.', log)

    def test_existing_files_are_preserved_and_not_revalidated_or_downloaded(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Existing files are not implicitly certified as valid or current.
            for year in batch.DEFAULT_YEARS:
                (root / f'intel_{year}_api_metrics_with_values.json').write_text('keep existing bytes')
            before = {p: p.read_bytes() for p in root.iterdir()}
            with patch.object(batch.pipeline, 'main', side_effect=AssertionError('No extraction')) as single:
                code, log = run(['--ticker', 'INTC', '--company', 'Intel', '--output-dir', tmp])
            self.assertEqual(code, 0, log)
            single.assert_not_called()
            self.assertIn('not revalidated', log)
            self.assertIn('0 created; 3 existing files kept; 0 failed.', log)
            self.assertEqual(before, {p: p.read_bytes() for p in root.iterdir()})

    def test_partial_rerun_only_attempts_missing_years(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / 'intc_2024_api_metrics_with_values.json'
            output.write_text('preserved')
            with patch.object(batch.pipeline, 'main', return_value=0) as single:
                code, log = run(['--ticker', 'INTC', '--output-dir', tmp])
            self.assertEqual(code, 0, log)
            self.assertEqual([c.args[0][-1] for c in single.call_args_list], ['2023', '2025'])
            self.assertEqual(output.read_text(), 'preserved')

    def test_directory_or_symlink_is_not_reported_as_an_existing_export(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'intc_2023_api_metrics_with_values.json').mkdir()
            (root / 'intc_2024_api_metrics_with_values.json').symlink_to(root / 'absent.json')
            with patch.object(batch.pipeline, 'main', return_value=1) as single:
                code, log = run(['--ticker', 'INTC', '--output-dir', tmp])
            self.assertEqual(code, 1)
            self.assertEqual(single.call_count, 3)
            self.assertIn('0 existing files kept', log)

    def test_invalid_input_does_not_start_extraction(self):
        with patch.object(batch.pipeline, 'main') as single:
            for args in (['--ticker', ' '], ['--ticker', 'INTC', '--years', '25'],
                         ['--ticker', 'INTC', '--years', 'not-a-year'], ['--company', 'Intel']):
                with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    batch.main(args)
            single.assert_not_called()

    def test_interrupt_stops_the_batch(self):
        with TemporaryDirectory() as tmp, \
                patch.object(batch.pipeline, 'main', side_effect=KeyboardInterrupt) as single:
            with redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                batch.main(['--ticker', 'INTC', '--output-dir', tmp])
            self.assertEqual(single.call_count, 1)

    def test_merge_runs_only_when_all_years_are_available(self):
        with TemporaryDirectory() as tmp:
            for year in batch.DEFAULT_YEARS:
                (Path(tmp) / f'intel_{year}_api_metrics_with_values.json').write_text('existing')
            with patch.object(batch.pipeline, 'main') as single, \
                    patch.object(merge_api_metrics, 'main', return_value=0) as merge:
                code, log = run(['--ticker', 'INTC', '--company', 'Intel', '--output-dir', tmp, '--merge'])
            self.assertEqual(code, 0, log)
            single.assert_not_called()
            merge.assert_called_once_with(['--company', 'Intel', '--years', '2023', '2024', '2025', '--input-dir', tmp])
        with TemporaryDirectory() as tmp, patch.object(batch.pipeline, 'main', side_effect=[0, 1, 0]), \
                patch.object(merge_api_metrics, 'main') as merge:
            code, log = run(['--ticker', 'INTC', '--output-dir', tmp, '--merge'])
            self.assertEqual(code, 1)
            merge.assert_not_called()
            self.assertIn('Merge skipped', log)

    @unittest.skipUnless(os.environ.get('SEC_API_METRICS_INTEGRATION') == '1', 'Requires cached Intel 2023–2025 inputs')
    def test_three_cached_intel_years_preserve_metric_data_and_write_only_three_outputs(self):
        baselines = {}
        for year in batch.DEFAULT_YEARS:
            path = Path(f'tests/for_table_development/intel/intel_{year}_api_metrics_with_values.json')
            if not path.is_file():
                self.skipTest(f'Missing baseline: {path}')
            baselines[year] = json.loads(path.read_text())
            source = baselines[year]['verification']['source']
            if not all(Path(source[k]).is_file() for k in ('filing', 'api_file')):
                self.skipTest(f'Missing cached input for {year}')
        original_main = batch.pipeline.main

        def cached_run(args):
            year = int(args[args.index('--year') + 1])
            source = baselines[year]['verification']['source']
            # Replace live selection only; use the unchanged real pipeline
            # with its full offline provenance, membership and export checks.
            return original_main(['--filing', source['filing'], '--xbrl-json', source['api_file'],
                                  '--offline', *args[2:]])

        with TemporaryDirectory() as tmp, patch.object(batch.pipeline, 'main', side_effect=cached_run), \
                patch.object(batch.pipeline.api, 'fetch_xbrl_json', side_effect=AssertionError('No API calls')):
            code, log = run(['--ticker', 'INTC', '--company', 'Intel', '--output-dir', tmp])
            self.assertEqual(code, 0, log)
            self.assertEqual(len(list(Path(tmp).iterdir())), 3)
            for year, baseline in baselines.items():
                output = json.loads((Path(tmp) / f'intel_{year}_api_metrics_with_values.json').read_text())
                for field in ('schema_version', 'metrics', 'counts'):
                    self.assertEqual(output[field], baseline[field])


if __name__ == '__main__':
    unittest.main()
