"""Split visual headings and an Item 8 statement inventory backed by a TOC."""
from copy import deepcopy
import unittest

import check_item8_api as check
import export_api_metrics as export
from test_api_filing_documents import document, fact, fixture, RESOURCES, ITEMS, URL, resolve
from test_query_item8_metrics import package


MD = "Management's Discussion and Analysis of Financial Condition and Results of Operations"
CONTROL = "Management's Annual Report on Internal Control Over Financial Reporting"
AUDIT = 'Report of Independent Registered Public Accounting Firm'
CONDITION = 'Consolidated Statements of Financial Condition'
INCOME = 'Consolidated Statements of Comprehensive Income (Loss)'
CASH = 'Consolidated Statements of Cash Flows'
NOTES = 'Notes to Consolidated Financial Statements'
QUARTER = 'Quarterly Financial Data (Unaudited)'


def heading(text):
    return '<div style="text-align:center"><b>' + text + '</b></div>'


def styled_fixture():
    _, _, payload = fixture()
    titles = [CONTROL, AUDIT, CONDITION + ' at December 31, 2023 and 2022',
              INCOME + ' for the years ended December 31, 2023 and 2022', CASH, NOTES]
    inventory = '<table>' + ''.join('<tr><td>' + t + '</td></tr>' for t in titles) + '</table>'
    primary = document('<h1>Item 1. Business</h1><p>' + fact(1) + '</p>'
        '<h1>Item 1A. Risk Factors</h1><p>Business risks.</p><h1>Item 7. Discussion</h1>'
        f'<p>The information under "{MD}" in our annual report (filed as Exhibit 13) is incorporated herein by reference.</p>'
        '<h1>Item 8. Financial Statements</h1><p>The following consolidated financial statements and the auditor report '
        'in our annual report (filed as Exhibit 13) are incorporated herein by reference.</p>' + inventory
        + f'<p>The data under "{QUARTER}" in our annual report (filed as Exhibit 13) is incorporated herein by reference.</p>'
        '<h1>Item 9. Accountants</h1><h1>Item 15. Exhibits</h1>'
        '<table><tr><td>13</td><td><a href="report.htm">Annual Report</a></td></tr></table>')
    toc_titles = [('Shareholder Letter', 1), (MD, 2),
        (CONTROL + ' (PCAOB ID <ix:nonNumeric name="dei:AuditorFirmId" contextRef="annual">173</ix:nonNumeric>)', 4),
        (AUDIT, 5), ('Consolidated Financial Statements', 6), (NOTES, 9), ('Quarterly Data', 11)]
    toc = '<table>' + ''.join(f'<tr><td>{title}</td><td>{page}</td></tr>' for title, page in toc_titles) + '</table>'
    def page(number, content):
        return f'<div style="break-before:page"/>{content}<div style="position:absolute;bottom:0">{number}</div>'
    def income(identifier):
        return '<table><tr><th>Metric</th><th>2023</th></tr><tr><td>Net income</td><td>' + fact(2, identifier) + '</td></tr></table>'
    report = document(toc + page(1, heading('Shareholder Letter') + '<p>Income: ' + fact(4, 'outside') + '</p>')
        + page(2, heading("Management's Discussion and Analysis of")
               + heading('Financial Condition and Results of Operations') + '<p>Income: ' + fact(1, 'review') + '</p>')
        + page(3, '<p>Review continues.</p>')
        + page(4, heading("Management's Annual Report on Internal Control Over") + heading('Financial Reporting') + '<p>Control report.</p>')
        + page(5, heading(AUDIT) + '<p>Auditor report.</p>')
        + page(6, heading(CONDITION) + income('condition'))
        + page(7, heading('Consolidated Statements of Comprehensive Income') + income('income'))
        + page(8, heading(CASH) + income('cash'))
        + page(9, heading(NOTES) + '<p>Profit: ' + fact(3, 'note', 'ProfitLoss') + '</p>')
        + page(10, heading(NOTES + ' − (Continued)') + '<p>More notes.</p>')
        + page(11, '<ix:nonNumeric name="us-gaap:QuarterlyFinancialInformationTextBlock" contextRef="annual">'
               + heading(QUARTER) + '<p>Income: ' + fact(5, 'quarter') + '</p></ix:nonNumeric>'), RESOURCES)
    template = payload['StatementsOfIncome']['NetIncomeLoss'][0]
    payload['StatementsOfIncome']['NetIncomeLoss'].append({**template, 'value': '5000000'})
    return primary, report, payload


class StyledReportTests(unittest.TestCase):
    def test_toc_split_titles_inventory_and_final_tagged_section(self):
        primary, report, payload = styled_fixture()
        original = deepcopy(payload)
        docs = resolve(primary, report)
        checked = check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023, items=ITEMS, documents=docs)
        _, output, audit = export.build_metrics(payload, primary, checked,
            export.taxonomy.build_dictionary(package(2023), 2023), 'api.json', None, documents=docs)
        metrics = {m['query_name']: m for m in output['metrics']}
        values = {v['value']: v for v in metrics['NetIncomeLoss']['value']}
        self.assertEqual(set(values), {'1000000', '2000000', '5000000'})
        self.assertEqual(values['1000000']['items'], ['1', '7'])
        self.assertEqual(values['2000000']['items'], ['8'])
        self.assertEqual({s['page'] for s in values['2000000']['source_labels']}, {'6', '7', '8'})
        self.assertEqual(values['5000000']['source_labels'][0]['page'], '11')
        self.assertEqual(audit['excluded_api_entries'], 1)
        scopes = docs['report1']['layout'].ranges
        self.assertEqual(len(scopes), 8)
        self.assertTrue(all(s['method'] == 'external_report_toc_section' for s in scopes))
        self.assertEqual(len(scopes[0]['heading_locators']), 2)
        self.assertEqual(scopes[-1]['end_method'], 'final_report_footer')
        self.assertTrue(all(s['index_evidence'] for s in scopes if s['item'] == '8'))
        self.assertEqual(payload, original)

    def test_item_seven_only_ends_at_the_next_verified_toc_section(self):
        primary, report, payload = styled_fixture()
        docs = resolve(primary, report, ('7',))
        checked = check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023, items=('7',), documents=docs)
        _, output, _ = export.build_metrics(payload, primary, checked,
            export.taxonomy.build_dictionary(package(2023), 2023), 'api.json', None, documents=docs)
        self.assertEqual([v['value'] for m in output['metrics'] for v in m['value']], ['1000000'])
        scope = docs['report1']['layout'].ranges[0]
        self.assertEqual(scope['end_method'], 'verified_heading')

    def test_ambiguous_or_unverified_layout_fails_closed(self):
        primary, report, _ = styled_fixture()
        first = heading("Management's Discussion and Analysis of").encode()
        variants = [
            (primary, report.replace(first, first + b'<p>Intervening prose.</p>'), 'referenced report headings'),
            (primary, report.replace(first, first + b'<div style="break-before:page"/>'), 'referenced report headings'),
            (primary, report.replace((MD + '</td><td>2').encode(), (MD + '</td><td>3').encode()), 'TOC pages'),
            (primary, report.replace(b'>10</div>', b'>9</div>'), 'verified printed-page TOC'),
            (primary, report.replace(b'</body>', b'<p>Unnumbered content after the final footer.</p></body>'), 'end of report section'),
            (primary, report.replace(heading(QUARTER).encode(), b'<p>Unrelated earlier content.</p>' + heading(QUARTER).encode()), 'referenced report headings'),
            (primary, report.replace(heading(QUARTER).encode(), heading('Different Data').encode()), 'referenced report headings'),
            (primary.replace(b'<td>Notes to Consolidated Financial Statements</td>', b'<td>Unrecognized inventory entry</td>'), report, 'inventory'),
            (primary.replace(b'<td>Consolidated Statements of Cash Flows</td>', ('<td>' + fact(2, 'bad_inventory') + '</td>').encode()), report, 'inventory'),
        ]
        for p, r, message in variants:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                resolve(p, r)


if __name__ == '__main__':
    unittest.main()
