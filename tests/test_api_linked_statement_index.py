"""External statement fragment links, linked TOCs and final-note boundaries."""
from copy import deepcopy
import unittest

from sec_disclosure.table_extraction import check_item8_api as check
from sec_disclosure.table_extraction import export_api_metrics as export
from test_api_filing_documents import document, fact, fixture, RESOURCES, ITEMS, URL, resolve
from test_api_styled_report import MD, AUDIT, CONDITION, INCOME, CASH, NOTES, heading
from test_query_item8_metrics import package


def linked_fixture():
    _, _, payload = fixture()
    titles = [(AUDIT, 4), (CONDITION, 5), (INCOME, 6), (CASH, 7), (NOTES, 8)]
    index = '<table>' + ''.join(f'<tr><td><a href="report.htm#s{page}">{title}</a></td>'
        f'<td/><td>{page}</td></tr>' for title, page in titles) + '</table>'
    primary = document('<h1>Item 1. Business</h1><p>Income: ' + fact(1) + '</p>'
        '<h1>Item 1A. Risk Factors</h1><p>Business risks.</p><h1>Item 7. Discussion</h1>'
        f'<p>The information under "{MD}" in our annual report is incorporated herein by reference.</p>'
        '<h1>Item 8. Financial Statements</h1><p>The consolidated financial statements, notes, auditor report and '
        'Selected Financial Data, which are listed under Item 15 herein, are included in the Annual Report '
        'and are incorporated herein by reference.</p><h1>Item 9. Accountants</h1><h1>Item 15. Exhibits</h1>'
        + index + '<table><tr><td>13</td><td><a href="report.htm">Annual Report</a></td></tr></table>')
    toc = '<table><tr><td>Market information</td><td>i</td></tr>' + ''.join(
        f'<tr><td><a href="#s{page}">{title}</a></td><td>{page}</td></tr>' for title, page in [
            ('Shareholder Letter', 1), ('Selected Consolidated Financial and Other Data', 2),
            (MD, 3), ('Consolidated Financial Statements', 5)]) + '</table>'
    def page(number, content):
        return f'<div style="break-before:page"/><a id="s{number}"/>{content}<div style="position:absolute;bottom:0">{number}</div>'
    report = document(toc + page(1, heading('Shareholder Letter') + '<p>Income: ' + fact(9, 'outside') + '</p>')
        + page(2, heading('Selected Consolidated Financial and Other Data') + '<p>Income: ' + fact(4, 'selected') + '</p>')
        + page(3, heading("Management's Discussion and Analysis of") + '<div/>'
               + heading('Financial Condition and Results of Operations') + '<p><!-- publisher -->Income: ' + fact(1, 'md') + '</p>')
        + page(4, f'<p style="text-align:center">{AUDIT}</p>')
        + page(5, heading(CONDITION) + '<p>Income: ' + fact(2, 'statement1') + '</p>')
        + page(6, heading(INCOME) + '<p>Income: ' + fact(2, 'statement2') + '</p>')
        + page(7, heading(CASH) + '<p>Income: ' + fact(2, 'statement3') + '</p>')
        + page(8, heading(NOTES) + '<ix:nonNumeric name="us-gaap:AccountingPoliciesTextBlock" contextRef="annual" '
            'continuedAt="last-note"><p>NOTE A - Accounting policies. Profit: ' + fact(3, 'note', 'ProfitLoss') + '</p></ix:nonNumeric>')
        + page(9, heading(NOTES + ' (Continued)') + '<ix:continuation id="last-note"><p>End of note.</p></ix:continuation>')
        + page(10, heading('Our employees') + '<p>Staff information.</p>'), RESOURCES)
    payload['StatementsOfIncome']['NetIncomeLoss'].append({**payload['StatementsOfIncome']['NetIncomeLoss'][0], 'value': '9000000'})
    return primary, report, payload


class LinkedStatementIndexTests(unittest.TestCase):
    def test_item15_links_and_toc_assign_exact_api_values(self):
        primary, report, payload = linked_fixture()
        original = deepcopy(payload)
        docs = resolve(primary, report)
        checked = check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023, items=ITEMS, documents=docs)
        _, output, audit = export.build_metrics(payload, primary, checked,
            export.taxonomy.build_dictionary(package(2023), 2023), 'api.json', None, documents=docs)
        income = next(m for m in output['metrics'] if m['query_name'] == 'NetIncomeLoss')
        self.assertEqual({v['value']: v['items'] for v in income['value']},
                         {'1000000': ['1', '7'], '2000000': ['8'], '4000000': ['8']})
        self.assertEqual(audit['excluded_api_entries'], 1)
        ranges = docs['report1']['layout'].ranges
        notes = next(r for r in ranges if r['section'] == NOTES)
        self.assertEqual(notes['end_method'], 'final_note_continuation')
        self.assertEqual(len(ranges), 7)
        md = next(r for r in ranges if r['item'] == '7')
        self.assertEqual(md['end'], min(r['start'] for r in ranges if r['method'] == 'external_report_linked_index'))
        self.assertEqual(original, payload)

    def test_invalid_links_pages_and_unbounded_notes_fail(self):
        primary, report, _ = linked_fixture()
        variants = [
            (primary.replace(b'report.htm#s5', b'other.htm#s5'), report),
            (primary.replace(b'report.htm#s5', b'https://example.com/report.htm#s5'), report),
            (primary.replace(b'<td>5</td>', b'<td>6</td>'), report),
            (primary, report.replace(b'id="s5"', b'id="missing"')),
            (primary, report.replace(b'continuedAt="last-note"', b'continuedAt="absent"')),
            (primary, report.replace(b'NOTE A - Accounting', b'Unidentified Accounting')),
            (primary, report.replace(b'Staff information.', ('Staff income: ' + fact(9, 'late-fact')).encode())),
            (primary, report.replace(b'Staff information.', b'<span style="-sec-ix-hidden:unknown">9</span>')),
            (primary, report.replace(b'>7</div>', b'>6</div>')),
        ]
        for p, r in variants:
            with self.subTest(index=variants.index((p, r))), self.assertRaises(ValueError):
                resolve(p, r)


if __name__ == '__main__':
    unittest.main()
