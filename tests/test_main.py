import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.main import draft_summary_line, main
from src.sttm import CSV_TEMPLATE

ROOT = Path(__file__).resolve().parent.parent


def run_cli(*args, last_run=None):
    last_run = last_run or Path(tempfile.mkdtemp()) / 'last_run.json'
    with mock.patch('sys.argv', ['src.main', *args]), mock.patch('src.main.LAST_RUN', last_run), \
            contextlib.redirect_stdout(io.StringIO()) as out:
        code = main()
    return code, out.getvalue()


class MainCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.report = self.tmp / 'report.json'
        self.defects = self.tmp / 'defects.json'

    def write(self, name, value):
        path = self.tmp / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')
        return str(path)

    def test_source_and_target_files_with_csv_sttm(self):
        source = self.write('source.json', [{'InvoiceNumber': 'INV1', 'Voucher': 'V1', 'Amount': '5.00', 'ApprovalStatus': 'Approved', 'DueDate': '2026-01-01'}])
        target = self.write('target.json', [{'invoice': {'number': 'INV1'}, 'voucher': 'V1', 'payment': {'amount': 5, 'dueDate': '2026-01-01'}, 'status': 'Approved'}])
        mapping = self.write('sttm.csv', CSV_TEMPLATE)
        code, output = run_cli('--source-file', source, '--target-file', target, '--mapping', mapping,
                               '--report', str(self.report), '--defects-report', str(self.defects))
        self.assertEqual(code, 0)
        self.assertIn('Validation status: PASS', output)
        self.assertEqual(json.loads(self.defects.read_text())['validation_status'], 'PASS')

    def test_defaults_write_defect_drafts(self):
        code, _ = run_cli('--source-file', str(ROOT / 'data' / 'sql_sample.json'),
                          '--target-file', str(ROOT / 'data' / 'cosmos_sample.json'),
                          '--mapping', str(ROOT / 'config' / 'mapping.json'),
                          '--report', str(self.report), '--defects-report', str(self.defects))
        self.assertEqual(code, 1)
        drafts = json.loads(self.defects.read_text())
        self.assertEqual(drafts['validation_status'], 'FAIL')
        self.assertEqual(len(drafts['defects']), 3)
        self.assertEqual(drafts['defects'][0]['preconditions'], ['Log in to the Azure portal.'])

    def test_db_names_flow_into_defect_drafts(self):
        run_cli('--source-file', str(ROOT / 'data' / 'sql_sample.json'),
                '--target-file', str(ROOT / 'data' / 'cosmos_sample.json'),
                '--mapping', str(ROOT / 'config' / 'mapping.json'),
                '--source-name', 'FinanceERP', '--target-name', 'receivables-db',
                '--report', str(self.report), '--defects-report', str(self.defects))
        description = json.loads(self.defects.read_text())['defects'][0]['defect_description']
        self.assertIn("present in Source DB 'FinanceERP' and in Target DB 'receivables-db'", description)

    def test_writes_row_by_row_comparison_csv(self):
        _, output = run_cli('--source-file', str(ROOT / 'data' / 'sql_sample.json'),
                            '--target-file', str(ROOT / 'data' / 'cosmos_sample.json'),
                            '--mapping', str(ROOT / 'config' / 'mapping.json'),
                            '--report', str(self.report), '--defects-report', str(self.defects))
        comparison = self.tmp / 'report.csv'
        self.assertIn(f'Row-by-row comparison: {comparison}', output)
        rows = list(csv.DictReader(io.StringIO(comparison.read_text(encoding='utf-8-sig'))))
        drafts = json.loads(self.defects.read_text())['defects']
        self.assertEqual({r['result'] for r in rows} >= {'MATCH', 'MISMATCH'}, True)
        self.assertEqual({i for r in rows for i in r['defect_id'].split(', ') if i}, {d['defect_id'] for d in drafts})
        self.assertIn('comparisons', json.loads(self.report.read_text()))

    def test_legacy_flag_aliases_still_work(self):
        code, _ = run_cli('--sql-file', str(ROOT / 'data' / 'sql_sample.json'),
                          '--cosmos-file', str(ROOT / 'data' / 'cosmos_sample.json'),
                          '--mapping', str(ROOT / 'config' / 'mapping.json'),
                          '--report', str(self.report), '--defects-report', str(self.defects))
        self.assertEqual(code, 1)

    def test_run_manifest_records_absolute_input_paths(self):
        manifest = self.tmp / 'last_run.json'
        source, target, mapping = (str(ROOT / 'data' / 'sql_sample.json'), str(ROOT / 'data' / 'cosmos_sample.json'),
                                   str(ROOT / 'config' / 'mapping.json'))
        run_cli('--source-file', source, '--target-file', target, '--mapping', mapping,
                '--report', str(self.report), '--defects-report', str(self.defects), last_run=manifest)
        recorded = json.loads(manifest.read_text())
        self.assertEqual(recorded['source_file'], str(Path(source).resolve()))
        self.assertEqual(recorded['mapping_file'], str(Path(mapping).resolve()))
        self.assertEqual(recorded['validation_report'], str(self.report.resolve()))
        self.assertEqual(recorded['defects_report'], str(self.defects.resolve()))
        self.assertIsNone(recorded['source_name'])

    def test_run_manifest_records_db_names(self):
        manifest = self.tmp / 'last_run.json'
        run_cli('--source-file', str(ROOT / 'data' / 'sql_sample.json'), '--target-file', str(ROOT / 'data' / 'cosmos_sample.json'),
                '--mapping', str(ROOT / 'config' / 'mapping.json'), '--source-name', 'abc', '--target-name', 'bcd',
                '--report', str(self.report), '--defects-report', str(self.defects), last_run=manifest)
        recorded = json.loads(manifest.read_text())
        self.assertEqual((recorded['source_name'], recorded['target_name']), ('abc', 'bcd'))

    def test_open_ui_opens_last_run_url(self):
        with mock.patch('src.main.ui_running', return_value=True), mock.patch('src.main.webbrowser.open') as browser:
            run_cli('--source-file', str(ROOT / 'data' / 'sql_sample.json'), '--target-file', str(ROOT / 'data' / 'cosmos_sample.json'),
                    '--mapping', str(ROOT / 'config' / 'mapping.json'), '--report', str(self.report),
                    '--defects-report', str(self.defects), '--open-ui')
        browser.assert_called_once_with('http://localhost:8501/?run=last')

    def test_open_ui_rejected_in_live_mode(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run_cli('--mode', 'live', '--open-ui')

    def test_prints_one_summary_line_per_draft(self):
        _, output = run_cli('--source-file', str(ROOT / 'data' / 'sql_sample.json'),
                            '--target-file', str(ROOT / 'data' / 'cosmos_sample.json'),
                            '--mapping', str(ROOT / 'config' / 'mapping.json'),
                            '--report', str(self.report), '--defects-report', str(self.defects))
        lines = [line for line in output.splitlines() if line.startswith('  DRAFT-')]
        drafts = json.loads(self.defects.read_text())['defects']
        self.assertEqual(len(lines), len(drafts))
        self.assertIn('  DRAFT-001 | "INV001" | FIELD_VALUE_MISMATCH | Amount (Source) -> payment.amount (Target) '
                      '| expected "1500.00" | actual 1400', lines)
        self.assertTrue(lines[1].startswith('  DRAFT-002 | "INV003" | MISSING_RECORD_IN_TARGET | '))

    def test_pass_prints_no_draft_lines(self):
        source = self.write('source.json', [{'InvoiceNumber': 'INV1', 'Voucher': 'V1', 'Amount': '5.00', 'ApprovalStatus': 'Approved', 'DueDate': '2026-01-01'}])
        target = self.write('target.json', [{'invoice': {'number': 'INV1'}, 'voucher': 'V1', 'payment': {'amount': 5, 'dueDate': '2026-01-01'}, 'status': 'Approved'}])
        _, output = run_cli('--source-file', source, '--target-file', target, '--mapping', self.write('sttm.csv', CSV_TEMPLATE),
                            '--report', str(self.report), '--defects-report', str(self.defects))
        self.assertNotIn('Drafts (', output)
        self.assertNotIn('DRAFT-', output)

    def test_incomplete_reasons_are_printed(self):
        row = {'InvoiceNumber': 'INV1', 'Voucher': 'V1', 'Amount': '5.00', 'ApprovalStatus': 'Approved', 'DueDate': '2026-01-01'}
        source = self.write('source.json', [row, dict(row)])
        target = self.write('target.json', [{'invoice': {'number': 'INV1'}, 'voucher': 'V1', 'payment': {'amount': 5, 'dueDate': '2026-01-01'}, 'status': 'Approved'}])
        _, output = run_cli('--source-file', source, '--target-file', target, '--mapping', self.write('sttm.csv', CSV_TEMPLATE),
                            '--report', str(self.report), '--defects-report', str(self.defects))
        reasons = json.loads(self.defects.read_text())['incomplete_reasons']
        self.assertTrue(reasons)
        for reason in reasons:
            self.assertIn(f'INCOMPLETE: {reason}', output)

    def test_summary_line_values(self):
        base = {'defect_id': 'DRAFT-009', 'business_key': 'K|1', 'issue_category': 'NULL_VALUE_MISMATCH',
                'affected_field': 'Brand (Source) -> brandName (Target)'}
        # An absent attribute and an explicit null stay distinct; a '|' inside a value can't split the line.
        line = draft_summary_line({**base, 'expected_value': '<attribute absent>', 'actual_value': None})
        self.assertEqual(line, 'DRAFT-009 | "K|1" | NULL_VALUE_MISMATCH | Brand (Source) -> brandName (Target) '
                               '| expected "<attribute absent>" | actual null')
        # A transformed field also shows the raw Source value and the rule.
        line = draft_summary_line({**base, 'expected_value': 'Unbranded', 'actual_value': None,
                                   'source_value': None, 'transformation': "trim | default('Unbranded')"})
        self.assertTrue(line.endswith('| expected "Unbranded" | actual null | source null rule "trim | default(\'Unbranded\')"'))
        # Long values are cut and marked; newlines are escaped, so a draft is always one line.
        line = draft_summary_line({**base, 'expected_value': 'x' * 200, 'actual_value': 'a\nb'})
        self.assertIn('... [truncated, 202 chars]', line)
        self.assertIn('actual "a\\nb"', line)
        self.assertNotIn('\n', line)

    def test_invalid_source_file_is_reported(self):
        source = self.write('source.json', {'not': 'a list'})
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            run_cli('--source-file', source, '--mapping', str(ROOT / 'config' / 'mapping.json'),
                    '--report', str(self.report), '--defects-report', str(self.defects))
        self.assertIn('Source file', err.getvalue())
        self.assertFalse(self.report.exists())


if __name__ == '__main__':
    unittest.main()
