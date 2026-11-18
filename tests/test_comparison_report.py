import csv
import io
import unittest

from src.comparison_report import COLUMNS, cell, comparison_columns, comparison_csv, comparison_rows
from src.defect_generator import build_defect_report
from src.validator import reconcile

MAPPING = {'key': {'source': 'id', 'target': 'id'}, 'fields': [
    {'source': 'name', 'target': 'h.name', 'type': 'string'},
    {'source': 'code', 'target': 'code', 'type': 'string', 'transformation': 'upper'}]}
SOURCE = [{'id': '1', 'name': 'a', 'code': 'x'},      # every field matches
          {'id': '2', 'name': None, 'code': 'y'},      # Source null, Target absent
          {'id': '3', 'name': 'c'},                    # absent Code stays absent; Target holds a value
          {'id': '5', 'name': '=1+1', 'code': 'z'},    # missing in Target
          {'id': '5', 'name': 'dup', 'code': 'z'}]     # duplicate key
TARGET = [{'id': '1', 'h': {'name': 'a'}, 'code': 'X'},
          {'id': '2', 'h': {}, 'code': 'Y'},
          {'id': '3', 'h': {'name': 'c'}, 'code': 'Q'},
          {'id': '9', 'h': {'name': True}, 'code': None}]  # extra in Target


def rows_for(source=SOURCE, target=TARGET, with_defects=True):
    result = reconcile(source, target, MAPPING)
    defects = build_defect_report(result, MAPPING, source, target) if with_defects else None
    return comparison_rows(result, defects)


class ComparisonReportTests(unittest.TestCase):
    def test_cell_text(self):
        self.assertEqual([cell(v) for v in (None, True, False, 3, 1.5, 'NaN', {'a': [1]})],
                         ['NULL', 'true', 'false', '3', '1.5', 'NaN', '{"a": [1]}'])

    def test_one_row_per_field_with_values_side_by_side(self):
        rows, complete = rows_for()
        self.assertTrue(complete)
        by = {(r['key'], r['source_field']): r for r in rows}
        self.assertEqual(by[('1', 'name')]['result'], 'MATCH')
        self.assertEqual((by[('1', 'code')]['source_value'], by[('1', 'code')]['transformation'],
                          by[('1', 'code')]['expected_value'], by[('1', 'code')]['target_value']), ('x', 'upper', 'X', 'X'))
        self.assertEqual((by[('2', 'name')]['source_value'], by[('2', 'name')]['target_value'], by[('2', 'name')]['result']),
                         ('NULL', '<attribute absent>', 'MISMATCH'))
        self.assertEqual((by[('3', 'code')]['source_value'], by[('3', 'code')]['expected_value']),
                         ('<attribute absent>', '<attribute absent>'))
        self.assertEqual((by[('5', 'name')]['target_value'], by[('5', 'name')]['result']), ('<no Target record>', 'MISSING IN TARGET'))
        self.assertEqual((by[('9', 'name')]['source_value'], by[('9', 'name')]['target_value']), ('<no Source record>', 'true'))
        self.assertEqual(by[('9', 'code')]['target_value'], 'NULL')

    def test_rows_link_to_their_defect_drafts(self):
        result = reconcile(SOURCE, TARGET, MAPPING)
        report = build_defect_report(result, MAPPING, SOURCE, TARGET)
        rows, _ = comparison_rows(result, report)
        ids = {d['defect_id'] for d in report['defects']}
        linked = {i for r in rows for i in r['defect_id'].split(', ') if i}
        self.assertEqual(linked, ids)  # every draft appears in the table
        self.assertTrue(all(not r['defect_id'] for r in rows if r['result'] == 'MATCH'))
        key_issue = [r for r in rows if r['result'].startswith('KEY ISSUE')]
        self.assertEqual([r['result'] for r in key_issue], ['KEY ISSUE (Source): duplicate business key'])
        self.assertTrue(key_issue[0]['defect_id'])

    def test_columns_and_csv(self):
        rows, _ = rows_for(with_defects=False)
        self.assertEqual(comparison_columns(rows, with_defects=False), [c for c in COLUMNS if c != 'defect_id'])
        plain = [dict(r, transformation='', expected_value='') for r in rows]
        self.assertNotIn('transformation', comparison_columns(plain))
        parsed = list(csv.DictReader(io.StringIO(comparison_csv(rows))))
        self.assertEqual(len(parsed), len(rows))
        self.assertIn("'=1+1", [r['source_value'] for r in parsed])  # a formula-like value is neutralised
        for row in parsed:
            for value in row.values():
                self.assertFalse(value.startswith(('=', '@', '+')), value)

    def test_report_without_comparisons_lists_the_differences(self):
        result = reconcile(SOURCE[:3], TARGET[:3], MAPPING)
        del result['comparisons']  # a report written before row-by-row comparisons were kept
        rows, complete = comparison_rows(result)
        self.assertFalse(complete)
        self.assertEqual({r['result'] for r in rows}, {'MISMATCH'})
        self.assertEqual({(r['key'], r['source_field']) for r in rows}, {('2', 'name'), ('3', 'code')})


if __name__ == '__main__':
    unittest.main()
