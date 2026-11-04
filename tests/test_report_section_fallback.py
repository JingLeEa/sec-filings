"""Named external-report recovery uses original IDs, safe ranges and PostgreSQL caches."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from artifact_support import MemoryArtifactStore
from sec_disclosure.agents import filing_item_fallback as fallback
from sec_disclosure.extraction import sec_10k_extractor as extraction
from sec_disclosure.llm.client import CompletionResult
from sec_disclosure.llm.config import LLMConfig
from sec_disclosure.storage.artifacts import PostgresWorkspace
from test_sec_10k_extractor import incorporated_filing, report_index


CONFIG = LLMConfig('https://example.test/v1', 'test-key', 'test-model')
PATHS = [['Operating Review', 'Risk Overview'], ['Operating Review'], ['Accounts'],
         ['Notes to the Accounts'], ['Quarterly Results']]
LEVELS = {'Operating Review': 0, 'Risk Overview': 1, 'Corporate Governance': 0,
          'Accounts': 0, 'Notes to the Accounts': 0, 'Quarterly Results': 0, 'Glossary': 0}
REPORT = '<html><body>' + ''.join(
    '<div style="font-weight:700">' + title + '</div><p>' + body + '</p>'
    for title, body in [
        ('Operating Review', 'Our revenue increased and demand improved.'),
        ('Risk Overview', 'Supply disruptions could affect our business.'),
        ('Corporate Governance', 'This governance text is outside the requested sections.'),
        ('Accounts', 'Our statements consolidate subsidiaries.'),
        ('Notes to the Accounts', 'Our tax expense changed.'),
        ('Quarterly Results', 'Quarterly figures were revised.'),
        ('Glossary', 'This glossary text is outside the requested sections.'),
    ]) + '</body></html>'


def response(prompt, **kwargs):
    decoder = json.JSONDecoder()
    targets, _ = decoder.raw_decode(prompt.split('\nRequested IDs: ', 1)[1])
    evidence, _ = decoder.raw_decode(prompt.split('\nSource evidence: ', 1)[1])
    ids = {title: identity for identity, title in targets.items()}
    rows = [{'source_id': row['source_id'], 'heading_last_source_id': row['source_id'],
             'title': row['text'], 'section_id': ids.get(row['text']), 'level': LEVELS[row['text']]}
            for row in evidence if row['text'] in LEVELS]
    return CompletionResult(json.dumps({'headings': rows}), 'test-model', 'stop',
                            {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150})


class ReportSectionFallbackTests(unittest.TestCase):
    def locate(self, *, events=None, paths=PATHS, html=REPORT, cache=None):
        return fallback.locate_report_sections(
            events or extraction.html_to_structure_events(html), paths, source='report.htm',
            source_html=html, config=CONFIG, cache_dir=cache, retries=0,
        )

    def test_outline_preserves_nested_sections_and_stops_at_unrequested_peer_headings(self):
        events = extraction.html_to_structure_events(REPORT)
        with self.assertRaisesRegex(ValueError, 'Cannot locate referenced report headings'):
            extraction.report_outline(events, PATHS)
        with patch.object(fallback, 'request_completion', side_effect=response) as api:
            result = self.locate(events=events)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(api.call_args.kwargs['system_prompt'], fallback.REPORT_SYSTEM)
        review = extraction.referenced_section_ranges(result.nodes, [['Operating Review']])[0]
        risk = extraction.referenced_section_ranges(result.nodes, [['Operating Review', 'Risk Overview']])[0]
        self.assertEqual(events[review['end']].text, 'Corporate Governance')
        self.assertLess(review['start'], risk['start'])
        self.assertEqual(review['end'], risk['end'])
        blocks = extraction.referenced_report_blocks(events, result.nodes, [review], [risk])
        self.assertIn('Our revenue increased', ' '.join(block.text for block in blocks))
        self.assertNotIn('Supply disruptions', ' '.join(block.text for block in blocks))

    def test_postgres_cache_replay_makes_no_requests_and_changed_sources_do_not_reuse_it(self):
        store = MemoryArtifactStore()
        with PostgresWorkspace('report-recovery-test', store=store) as workspace:
            cache = workspace.root / 'filing_page_cache'
            with patch.object(fallback, 'request_completion', side_effect=response) as api:
                original = self.locate(cache=cache)
                self.assertEqual(api.call_count, 1)
            with patch.object(fallback, 'request_completion', side_effect=AssertionError('No paid replay')):
                replay = self.locate(cache=cache)
            self.assertEqual(replay.nodes, original.nodes)
            self.assertEqual((replay.attempts, replay.cache_hits), (0, 1))
            with patch.object(fallback, 'request_completion', side_effect=response) as api:
                self.locate(cache=cache, html=REPORT + '\n')
                self.assertEqual(api.call_count, 1)
            self.assertFalse(Path(str(workspace.root)).exists())
        self.assertTrue(any(key.endswith('/result.json') for key in store.rows))

    def test_invalid_source_ids_rewritten_titles_and_bad_levels_are_rejected(self):
        events = extraction.html_to_structure_events(REPORT)
        evidence = fallback.report_heading_evidence(events)
        targets = {'S0': 'Operating Review'}
        row = {'source_id': evidence[0]['source_id'], 'heading_last_source_id': evidence[0]['source_id'],
               'title': 'Operating Review', 'section_id': 'S0', 'level': 0}
        variants = [dict(row, source_id=999999), dict(row, title='Invented report text'),
                    dict(row, level=True), dict(row, level=9), dict(row, section_id='S999'),
                    dict(row, heading_last_source_id=evidence[-1]['source_id'])]
        for invalid in variants:
            with self.subTest(invalid=invalid), self.assertRaises(fallback.InvalidFallbackResponse):
                fallback._validate_report_headings({'headings': [invalid]}, evidence, events, targets)

    def test_company_prefix_alias_keeps_the_original_body_heading_and_rejects_wrong_statement(self):
        events = extraction.html_to_structure_events(
            '<div><b>Consolidated Statement of Income</b></div><p>Income changed.</p>'
            '<div><b>Consolidated Statement of Comprehensive Income</b></div><p>Other income changed.</p>')
        evidence = fallback.report_heading_evidence(events)
        targets = {'S0': 'Example Bancorp Consolidated Statement of Income',
                   'S1': 'Example Bancorp Consolidated Statement of Comprehensive Income'}
        rows = [{'source_id': row['source_id'], 'heading_last_source_id': row['source_id'],
                 'title': row['text'], 'section_id': f'S{index}', 'level': 0}
                for index, row in enumerate(row for row in evidence if row['bold'])]
        nodes = fallback._report_nodes(rows, evidence, events, targets, [[title] for title in targets.values()])
        self.assertEqual(nodes[0]['title'], 'Consolidated Statement of Income')
        self.assertEqual(fallback._report_title_candidates(evidence, targets['S0']), [
            {'source_id': rows[0]['source_id'], 'title': 'Consolidated Statement of Income'}])
        invalid = deepcopy(rows)
        invalid[0]['section_id'] = 'S1'
        with self.assertRaises(fallback.InvalidFallbackResponse):
            fallback._validate_report_headings({'headings': invalid}, evidence, events, targets)

    def test_invalid_hierarchy_does_not_publish_a_result_or_poison_later_recovery(self):
        def bad_response(prompt, **kwargs):
            result = response(prompt, **kwargs)
            payload = json.loads(result.text)
            for row in payload['headings']:
                if row['title'] == 'Risk Overview':
                    row['level'] = 0
            return CompletionResult(json.dumps(payload), result.model, result.finish_reason, result.usage)

        store = MemoryArtifactStore()
        with PostgresWorkspace('report-recovery-test', store=store) as workspace:
            with patch.object(fallback, 'request_completion', side_effect=bad_response), self.assertRaises(ValueError):
                self.locate(cache=workspace.root)
            self.assertFalse(any(key.endswith('/result.json') for key in store.rows))
            with patch.object(fallback, 'request_completion', side_effect=response) as api:
                self.locate(cache=workspace.root)
                self.assertEqual(api.call_count, 1)

    def test_missing_requested_heading_is_repaired_with_whole_outline_feedback(self):
        calls = []

        def incomplete_then_complete(prompt, **kwargs):
            calls.append(prompt)
            result = response(prompt, **kwargs)
            payload = json.loads(result.text)
            payload['headings'] = [row for row in payload['headings']
                                   if (row['title'] == 'Quarterly Results') == (len(calls) > 1)]
            return CompletionResult(json.dumps(payload), result.model, result.finish_reason, result.usage)

        with patch.object(fallback, 'request_completion', side_effect=incomplete_then_complete):
            result = self.locate()
        self.assertEqual(result.attempts, 2)
        self.assertIn('Missing referenced report headings: Quarterly Results', calls[1])
        self.assertTrue(extraction.referenced_section_ranges(result.nodes, [['Quarterly Results']]))
        # The repair returns ONLY the missing heading. Other requested headings
        # and unrequested closing boundaries must survive, including Risk Overview.
        extraction.referenced_section_ranges(result.nodes, PATHS)
        review = extraction.referenced_section_ranges(result.nodes, [['Operating Review']])[0]
        events = extraction.html_to_structure_events(REPORT)
        self.assertEqual(events[review['end']].text, 'Corporate Governance')

    def test_repair_retries_a_locally_valid_reply_that_still_omits_its_required_heading(self):
        calls = []

        def omission_then_patch(prompt, **kwargs):
            calls.append(prompt)
            original = response(prompt, **kwargs)
            payload = json.loads(original.text)
            if len(calls) == 1:
                payload['headings'] = [row for row in payload['headings'] if row['title'] != 'Quarterly Results']
            elif len(calls) == 2:
                payload['headings'] = []
            else:
                payload['headings'] = [row for row in payload['headings'] if row['title'] == 'Quarterly Results']
            return CompletionResult(json.dumps(payload), original.model, original.finish_reason, original.usage)

        with patch.object(fallback, 'request_completion', side_effect=omission_then_patch):
            result = fallback.locate_report_sections(
                extraction.html_to_structure_events(REPORT), PATHS, source='report.htm',
                source_html=REPORT, config=CONFIG, retries=1)
        self.assertEqual(result.attempts, 3)
        self.assertIn('Repair still missing referenced report headings: Quarterly Results', calls[2])
        extraction.referenced_section_ranges(result.nodes, PATHS)

    def test_repair_cannot_substitute_a_similarly_named_subsection_for_the_unique_heading(self):
        html = REPORT.replace('</body>', '<div><b>Additional Quarterly Results</b></div></body>')
        events = extraction.html_to_structure_events(html)
        additional = next(event.index for event in events if event.text == 'Additional Quarterly Results')
        calls = []

        def wrong_then_correct(prompt, **kwargs):
            calls.append(prompt)
            original = response(prompt, **kwargs)
            payload = json.loads(original.text)
            quarter = next(row for row in payload['headings'] if row['title'] == 'Quarterly Results')
            if len(calls) == 1:
                payload['headings'] = [row for row in payload['headings'] if row['title'] != 'Quarterly Results']
            elif len(calls) == 2:
                payload['headings'] = [{**quarter, 'source_id': additional, 'heading_last_source_id': additional,
                                        'title': 'Additional Quarterly Results'}]
            else:
                payload['headings'] = [quarter]
            return CompletionResult(json.dumps(payload), original.model, original.finish_reason, original.usage)

        with patch.object(fallback, 'request_completion', side_effect=wrong_then_correct):
            result = fallback.locate_report_sections(events, PATHS, source='report.htm', source_html=html,
                                                      config=CONFIG, retries=1)
        self.assertEqual(result.attempts, 3)
        self.assertIn('Repair still missing referenced report headings: Quarterly Results', calls[2])
        selected = extraction.referenced_section_ranges(result.nodes, [['Quarterly Results']])[0]
        self.assertEqual(events[selected['start']].text, 'Quarterly Results')

    def test_failed_repair_keeps_valid_postgres_windows_for_resume(self):
        def omit_quarter(prompt, **kwargs):
            original = response(prompt, **kwargs)
            payload = json.loads(original.text)
            payload['headings'] = [row for row in payload['headings'] if row['title'] != 'Quarterly Results']
            return CompletionResult(json.dumps(payload), original.model, original.finish_reason, original.usage)

        def quarter_only(prompt, **kwargs):
            original = response(prompt, **kwargs)
            payload = json.loads(original.text)
            payload['headings'] = [row for row in payload['headings'] if row['title'] == 'Quarterly Results']
            return CompletionResult(json.dumps(payload), original.model, original.finish_reason, original.usage)

        store = MemoryArtifactStore()
        with PostgresWorkspace('report-recovery-test', store=store) as workspace:
            with patch.object(fallback, 'request_completion', side_effect=omit_quarter), \
                 self.assertRaisesRegex(fallback.InvalidFallbackResponse, 'Repair still missing'):
                self.locate(cache=workspace.root)
            key = next(key for key in store.rows if key.endswith('/window_000.json'))
            self.assertFalse(json.loads(store.get(key)[1]).get('invalid'))
            self.assertFalse(any(key.endswith('/result.json') for key in store.rows))
            trace = next(key for key in store.rows if key.endswith('/repair_000.json'))
            self.assertTrue(json.loads(store.get(trace)[1])['invalid'])
            with patch.object(fallback, 'request_completion', side_effect=quarter_only) as api:
                result = self.locate(cache=workspace.root)
            self.assertEqual(api.call_count, 1)
            self.assertEqual((result.attempts, result.cache_hits), (1, 1))
            extraction.referenced_section_ranges(result.nodes, PATHS)
            self.assertTrue(any(key.endswith('/initial_000.json') for key in store.rows))
            with patch.object(fallback, 'request_completion', side_effect=AssertionError('No API replay')):
                replay = self.locate(cache=workspace.root)
            self.assertEqual(replay.nodes, result.nodes)

    def test_repair_cannot_erase_a_requested_mapping_but_can_correct_its_level(self):
        previous = [{'source_id': 12, 'heading_last_source_id': 12, 'title': 'Risk Factors',
                     'level': 0, 'section_id': 'S0'}]
        repaired = fallback._merge_report_headings(previous, [{**previous[0], 'section_id': None, 'level': 1}])
        self.assertEqual((repaired[0]['section_id'], repaired[0]['level']), ('S0', 1))
        self.assertEqual(previous[0]['level'], 0)

    def test_prose_mentions_cannot_become_a_notes_heading_or_a_text_match_hint(self):
        prose = ('none of these events were required to be recognized or disclosed in the '
                 'consolidated financial statements and related notes.')
        events = extraction.html_to_structure_events('<p>' + prose + '</p><div><b>' + prose + '</b></div>')
        evidence = fallback.report_heading_evidence(events)
        self.assertNotIn(events[0].index, {row['source_id'] for row in evidence})
        target = 'Notes to Consolidated Financial Statements'
        self.assertEqual(fallback._report_title_candidates(evidence, target), [])
        row = {'source_id': events[1].index, 'heading_last_source_id': events[1].index,
               'title': prose, 'level': 0, 'section_id': 'S0'}
        with self.assertRaisesRegex(fallback.InvalidFallbackResponse, 'cannot map'):
            fallback._validate_report_headings({'headings': [row]}, evidence, events, {'S0': target})

    def test_nested_risk_subheading_does_not_duplicate_its_verified_parent_alias(self):
        html = ('<div><b>Risk Factors</b></div><p>Business risks.</p>'
                '<div><b>General Risk Factors</b></div><p>Other risks.</p>'
                '<div><b>Managing Committee</b></div><p>Directory.</p>')
        events = extraction.html_to_structure_events(html)
        evidence = fallback.report_heading_evidence(events)
        rows = [{'source_id': row['source_id'], 'heading_last_source_id': row['source_id'], 'title': row['text'],
                 'level': 1 if row['text'] == 'General Risk Factors' else 0,
                 'section_id': None if row['text'] == 'Managing Committee' else 'S0'} for row in evidence]
        nodes = fallback._report_nodes(rows, evidence, events, {'S0': 'Risk Factors'}, [['Risk Factors']])
        risk = extraction.referenced_section_ranges(nodes, [['Risk Factors']])[0]
        self.assertEqual(events[risk['start']].text, 'Risk Factors')
        self.assertEqual(events[risk['end']].text, 'Managing Committee')
        child = next(node for node in nodes if node['title'] == 'General Risk Factors')
        self.assertEqual(child['key'], extraction.report_title_key('General Risk Factors'))
        self.assertEqual(child['level'], 1)

    def test_disjoint_identical_headings_still_fail_as_ambiguous(self):
        html = '<div><b>Risk Factors</b></div><div><b>Other Section</b></div><div><b>Risk Factors</b></div>'
        events = extraction.html_to_structure_events(html)
        evidence = fallback.report_heading_evidence(events)
        rows = [{'source_id': row['source_id'], 'heading_last_source_id': row['source_id'], 'title': row['text'],
                 'level': 0, 'section_id': 'S0' if row['text'] == 'Risk Factors' else None} for row in evidence]
        with self.assertRaisesRegex(ValueError, 'Ambiguous referenced report heading'):
            fallback._report_nodes(rows, evidence, events, {'S0': 'Risk Factors'}, [['Risk Factors']])

    def test_prominent_boundary_omissions_are_repaired_without_forcing_every_heading_to_be_a_peer(self):
        titles = [('Financial Data', 16), ('Quarterly Details', 16), ('Company Information', 16),
                  ('Risk Factors', 12), ('Managing Committee', 16)]
        html = ''.join(f'<div style="font-size:{font}pt;font-weight:700">{title}</div><p>Body text.</p>'
                       for title, font in titles)
        events = extraction.html_to_structure_events(html)
        paths = [['Financial Data'], ['Risk Factors']]
        calls = []

        def partial_then_boundaries(prompt, **kwargs):
            calls.append(prompt)
            decoder = json.JSONDecoder()
            targets, _ = decoder.raw_decode(prompt.split('\nRequested IDs: ', 1)[1])
            evidence, _ = decoder.raw_decode(prompt.split('\nSource evidence: ', 1)[1])
            ids = {title: identity for identity, title in targets.items()}
            wanted = {'Financial Data', 'Managing Committee'} if len(calls) == 1 else {
                'Quarterly Details', 'Company Information', 'Risk Factors'}
            rows = [{'source_id': row['source_id'], 'heading_last_source_id': row['source_id'],
                     'title': row['text'], 'section_id': ids.get(row['text']),
                     'level': 1 if row['text'] == 'Quarterly Details' else 0}
                    for row in evidence if row['text'] in wanted]
            return CompletionResult(json.dumps({'headings': rows}), 'test-model', 'stop', {})

        with patch.object(fallback, 'request_completion', side_effect=partial_then_boundaries):
            result = fallback.locate_report_sections(events, paths, source='report.htm', source_html=html,
                                                      config=CONFIG, retries=0)
        self.assertEqual(result.attempts, 2)
        self.assertIn('Missing referenced report headings: Risk Factors', calls[1])
        self.assertIn('Company Information', calls[1])
        financial = extraction.referenced_section_ranges(result.nodes, [['Financial Data']])[0]
        self.assertEqual(events[financial['end']].text, 'Company Information')
        quarter = next(node for node in result.nodes if node['title'] == 'Quarterly Details')
        self.assertEqual(quarter['level'], 1)
        self.assertLess(quarter['start'], financial['end'])

    def test_repair_retries_when_a_prominent_boundary_is_still_omitted(self):
        html = ('<div style="font-size:16pt;font-weight:700">Financial Data</div><p>Results.</p>'
                '<div style="font-size:16pt;font-weight:700">Company Information</div><p>Directory.</p>')
        events = extraction.html_to_structure_events(html)
        calls = []

        def omit_then_fix(prompt, **kwargs):
            calls.append(prompt)
            index = 0 if len(calls) == 1 else 2
            headings = [] if len(calls) == 2 else [{
                'source_id': events[index].index, 'heading_last_source_id': events[index].index,
                'title': events[index].text, 'level': 0, 'section_id': 'S0' if len(calls) == 1 else None}]
            return CompletionResult(json.dumps({'headings': headings}), 'test-model', 'stop', {})

        with patch.object(fallback, 'request_completion', side_effect=omit_then_fix):
            result = fallback.locate_report_sections(events, [['Financial Data']], source='report.htm', source_html=html,
                                                      config=CONFIG, retries=1)
        self.assertEqual(result.attempts, 3)
        self.assertIn('Repair still missing section boundaries: Company Information', calls[2])
        selected = extraction.referenced_section_ranges(result.nodes, [['Financial Data']])[0]
        self.assertEqual(events[selected['end']].text, 'Company Information')

    def test_windows_preserve_unrequested_end_boundaries_and_recover_a_corrupt_checkpoint(self):
        titles = [f'Section {index} Business Review' for index in range(130)]
        html = ''.join(f'<div><b>{title}</b></div><p>{"Explanatory narrative. " * 12}</p>' for title in titles)
        paths = [[titles[0]], [titles[98]]]

        def all_headings(prompt, **kwargs):
            decoder = json.JSONDecoder()
            targets, _ = decoder.raw_decode(prompt.split('\nRequested IDs: ', 1)[1])
            evidence, _ = decoder.raw_decode(prompt.split('\nSource evidence: ', 1)[1])
            ids = {title: identity for identity, title in targets.items()}
            headings = [{'source_id': row['source_id'], 'heading_last_source_id': row['source_id'],
                         'title': row['text'], 'section_id': ids.get(row['text']), 'level': 0}
                        for row in evidence if row['text'] in titles]
            return CompletionResult(json.dumps({'headings': headings}), 'test-model', 'stop', {})

        store = MemoryArtifactStore()
        with PostgresWorkspace('report-recovery-test', store=store) as workspace:
            with patch.object(fallback, 'request_completion', side_effect=all_headings) as api:
                original = self.locate(paths=paths, html=html, cache=workspace.root)
                requests = api.call_count
            self.assertGreater(requests, 1)
            events = extraction.html_to_structure_events(html)
            for title in (titles[0], titles[98]):
                selected = extraction.referenced_section_ranges(original.nodes, [[title]])[0]
                self.assertEqual(events[selected['end']].text, titles[titles.index(title) + 1])
            # Simulate interruption before final publication and one corrupt window.
            result_key = next(key for key in store.rows if key.endswith('/result.json'))
            store.delete(result_key)
            window_key = next(key for key in store.rows if key.endswith('/window_000.json'))
            store.put(window_key, b'not JSON')
            with patch.object(fallback, 'request_completion', side_effect=all_headings) as api:
                replay = self.locate(paths=paths, html=html, cache=workspace.root)
                self.assertEqual(api.call_count, 1)
            self.assertEqual(replay.nodes, original.nodes)
            self.assertEqual(replay.cache_hits, requests - 1)

    def test_wrapped_heading_uses_adjacent_original_source_events(self):
        html = '<div><b>Example Bancorp</b></div><div><b>Consolidated Balance Sheet</b></div><p>Assets changed.</p>'
        events = extraction.html_to_structure_events(html)
        evidence = fallback.report_heading_evidence(events)
        title = 'Example Bancorp Consolidated Balance Sheet'
        headings = [{'source_id': events[0].index, 'heading_last_source_id': events[1].index,
                     'title': title, 'section_id': 'S0', 'level': 0}]
        nodes = fallback._report_nodes(headings, evidence, events, {'S0': title}, [[title]])
        self.assertEqual((nodes[0]['start'], nodes[0]['heading_end']), (0, 2))
        self.assertEqual(nodes[0]['source_heading'], title)

    def test_evidence_excludes_running_folios_while_preserving_real_body_headings(self):
        events = extraction.html_to_structure_events(
            '<div><b>64 U.S. Bancorp 2023 Annual Report</b></div>'
            '<div><b>Risk Factors</b></div><p>A risk could affect revenue.</p>'
            '<div><b>Company Information</b></div><p>Contact details.</p>'
            '<div><b>MD&amp;A | 18</b></div>')
        evidence = fallback.report_heading_evidence(events)
        titles = {row['text'] for row in evidence}
        self.assertTrue({'Risk Factors', 'Company Information'} <= titles)
        self.assertNotIn('64 U.S. Bancorp 2023 Annual Report', titles)
        self.assertNotIn('MD&A | 18', titles)

    def test_deterministic_report_does_not_call_the_fallback(self):
        html = '<h1>Operating Review</h1><p>Revenue increased.</p>'
        sections = {'7': extraction.html_to_blocks(
            '<p>The information in response to this Item appears under "Operating Review" '
            'in our Annual Report and is incorporated by reference.</p>')}
        with patch.object(extraction, 'discover_referenced_report', return_value=('report.htm', {
                'type': 'EX-13', 'index_source': 'index.htm'})), \
             patch.object(extraction, 'read_html_source', return_value=html), \
             patch.object(fallback, 'locate_report_sections', side_effect=AssertionError('Unnecessary LLM request')):
            result, audit = extraction.follow_report_references(
                sections, 'filing.htm', 'test', report_fallback=lambda *args: fallback.locate_report_sections(*args))
        self.assertIn('Revenue increased', ' '.join(block.text for block in result['7']))
        self.assertNotIn('boundary_recovery', audit['7'])

    def test_extractor_uses_external_report_fallback_and_records_audit_while_opt_out_is_preserved(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, report = root / 'wrapper.htm', root / 'financial-data.htm'
            source.write_text(incorporated_filing('2031'))
            report.write_text(REPORT)
            (root / '0000999888-32-000001-index.htm').write_text(report_index(report.name))
            args = [str(source), '--company', 'example', '--year', '2031', '--out-dir', str(root / 'output'),
                    '--agent-page-cache-dir', str(root / 'cache')]
            with patch('sec_disclosure.llm.config.load_config', return_value=CONFIG), \
                 patch.object(fallback, 'request_completion', side_effect=response) as api, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(extraction.main(args), 0)
                self.assertEqual(api.call_count, 1)
            output = root / 'output/example/2031'
            text = (output / '2031_item_7.txt').read_text()
            self.assertIn('Our revenue increased', text)
            self.assertNotIn('Supply disruptions', text)
            self.assertNotIn('governance text', text)
            audit = json.loads((output / '2031_sources.json').read_text())['referenced_items']
            self.assertEqual(audit['7']['boundary_recovery']['method'], fallback.REPORT_POLICY)
            self.assertEqual(audit['7']['source'], str(report))
            before = (output / '2031_chunks.json').read_bytes()
            with patch.object(fallback, 'request_completion', side_effect=AssertionError('Disabled fallback')), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(extraction.main([*args, '--no-agent-fallback']), 2)
            self.assertEqual((output / '2031_chunks.json').read_bytes(), before)
