"""A qualified TOC audit title must match its split body heading precisely."""
import unittest

from sec_disclosure.table_extraction import filing_sections as sections
from sec_disclosure.table_extraction import extract_10k_tables_xbrl as ix
from test_api_filing_documents import document, fact, RESOURCES


AUDIT = 'Report of Independent Registered Public Accounting Firm'
NOTES = 'Notes to Financial Statements'
OPINION = 'Opinion on the Consolidated Financial Statements'
CONTROL = 'Opinion on Internal Control Over Financial Reporting'


def report(audit_as_table=True):
    titles = [('Financial Review', True), (AUDIT + ' - ' + CONTROL, False),
              (NOTES, True), (AUDIT + ' - ' + OPINION, True), ('Quarterly Financial Data', True)]
    toc = '<table>' + ''.join('<tr><td>' + ('<b>' + title + '</b>' if bold else title)
                              + '</td></tr>' for title, bold in titles) + '</table>'
    heading = ('<table><tr><td><b>' + AUDIT + '</b></td></tr></table>' if audit_as_table
               else '<div><b>' + AUDIT + '</b></div>')
    return (toc + '<div>Financial Review</div><div><b>' + AUDIT + '</b></div>'
            '<div>To the Stockholders and Board of Directors</div><div>Example Company:</div>'
            '<div>' + CONTROL + '</div><p>Control audit.</p><div>' + NOTES + '</div>'
            '<p>The ' + AUDIT + ' also discussed these notes.</p><p>Last accounting note.</p>'
            + heading + '<div>To the Stockholders and Board of Directors</div><div>Example Company:</div>'
            '<div>' + OPINION + '</div><p>Auditor opinion, not an accounting note.</p>'
            '<div>Quarterly Financial Data</div><p>Quarterly values.</p>')


class AuditBoundaryTests(unittest.TestCase):
    def test_narrative_notes_stop_at_split_audit_heading_without_confusing_internal_controls(self):
        for table in (False, True):
            with self.subTest(table=table):
                events = sections.html_to_structure_events(report(table))
                outline = sections.report_outline(events, [[NOTES], [AUDIT + ' - ' + OPINION]])
                notes = sections.referenced_section_ranges(outline, [[NOTES]])[0]
                self.assertEqual(events[notes['end']].text, AUDIT)
                self.assertIn('Last accounting note.', ' '.join(e.text for e in events[notes['start']:notes['end']]))
                self.assertEqual(len([n for n in outline if n['key'].startswith('report of independent')]), 2)

    def test_financial_membership_uses_the_same_boundary(self):
        doc = ix.Document(document(report() + '<p>' + fact(1) + '</p>', RESOURCES), 'report.htm')
        layout = ix.Layout(doc)
        ix.attach_report_ranges(layout, [{'item': '8', 'section_paths': [[NOTES]], 'reference_text': 'Annual Report'}])
        scope = layout.ranges[-1]
        auditor = next(node for node in doc.tables if doc.display(node) == AUDIT)
        self.assertEqual(scope['end'], doc.order[auditor])
        self.assertEqual(scope['method'], 'external_report_section')

    def test_unqualified_audit_title_without_matching_opinion_does_not_guess(self):
        events = sections.html_to_structure_events(report().replace('<div>' + OPINION + '</div>',
                                                                    '<div>Unrelated discussion</div>'))
        with self.assertRaisesRegex(ValueError, 'Cannot locate referenced report headings'):
            sections.report_outline(events, [[NOTES], [AUDIT + ' - ' + OPINION]])
