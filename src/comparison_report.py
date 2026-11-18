"""Row-by-row comparison table of a reconciliation result.

One row per mapped field of every record: the Source value next to the Target value (and, for a
field with an STTM transformation, the rule and the value Target should hold), the result, and the
defect draft raised for it. Used for the UI's validation results and reports/validation_report.csv.
"""
import csv
import io
import json
from decimal import Decimal

from .defect_generator import (ABSENT, DUPLICATE_BUSINESS_KEY, EXTRA_IN_TARGET as EXTRA_DEFECT,
                               MISSING_IN_TARGET as MISSING_DEFECT, _csv_safe)
from .validator import EXTRA_IN_TARGET, MATCH, MISMATCH, MISSING_IN_TARGET

COLUMNS = ['key', 'source_field', 'target_field', 'source_value', 'transformation', 'expected_value',
           'target_value', 'result', 'defect_id']
NO_SOURCE_RECORD = '<no Source record>'
NO_TARGET_RECORD = '<no Target record>'


def cell(value):
    """Text for one value: null as NULL, booleans as true/false, objects and arrays as JSON."""
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return json.dumps(value, default=str, ensure_ascii=False)


def _defect_ids(defect_report):
    """Map (key, affected field) and (key, category) to the defect drafts raised for them."""
    by_field, by_record = {}, {}
    for draft in (defect_report or {}).get('defects', []):
        key, category = str(draft.get('business_key')), draft.get('issue_category')
        if category in (MISSING_DEFECT, EXTRA_DEFECT, DUPLICATE_BUSINESS_KEY):
            by_record.setdefault((key, category), []).append(draft['defect_id'])
        else:
            by_field.setdefault((key, draft.get('affected_field')), []).append(draft['defect_id'])
    return by_field, by_record


def comparison_rows(result, defect_report=None):
    """Return (rows, complete): one dict of display text per row, in COLUMNS order.

    complete is False for a report written before row-by-row comparisons were kept; its rows then
    list only the fields that differ.
    """
    by_field, by_record = _defect_ids(defect_report)
    comparisons = result.get('comparisons')
    complete = comparisons is not None
    if not complete:
        comparisons = [{'key': m['key'], **d, 'result': MISMATCH}
                       for m in result.get('mismatches', []) for d in m.get('differences', [])]
    rows = []
    for comparison in comparisons:
        key, status = str(comparison['key']), comparison['result']
        sf, tf = comparison.get('source_field'), comparison.get('target_field')
        row = {'key': key, 'source_field': sf, 'target_field': tf, 'result': status}
        row['source_value'] = (NO_SOURCE_RECORD if status == EXTRA_IN_TARGET else ABSENT if comparison.get('source_absent')
                               else cell(comparison.get('source_value')))
        row['target_value'] = (NO_TARGET_RECORD if status == MISSING_IN_TARGET else ABSENT if comparison.get('target_absent')
                               else cell(comparison.get('target_value')))
        if 'transformation' in comparison:
            row['transformation'] = comparison['transformation']
            if 'transformation_error' in comparison:
                row['expected_value'] = f"<transformation failed: {comparison['transformation_error']}>"
            elif 'expected_value' in comparison:
                expected = comparison['expected_value']
                # A rule leaves an absent Source attribute absent, as in the defect drafts.
                row['expected_value'] = ABSENT if expected is None and comparison.get('source_absent') else cell(expected)
        if status == MISSING_IN_TARGET:
            ids = by_record.get((key, MISSING_DEFECT), [])
        elif status == EXTRA_IN_TARGET:
            ids = by_record.get((key, EXTRA_DEFECT), [])
        else:
            ids = by_field.get((key, f'{sf} (Source) -> {tf} (Target)'), [])
        row['defect_id'] = ', '.join(ids)
        rows.append(row)
    for side, issues in (result.get('key_issues') or {}).items():
        for issue in issues:
            key = '' if issue.get('key') is None else str(issue['key'])
            ids = by_record.get((key, DUPLICATE_BUSINESS_KEY), [])
            rows.append({'key': key, 'result': f"KEY ISSUE ({side.capitalize()}): {issue.get('reason')}",
                         'defect_id': ', '.join(ids)})
    return [{column: row.get(column, '') for column in COLUMNS} for row in rows], complete


def comparison_columns(rows, with_defects=True):
    """COLUMNS without the transformation columns when no field has a rule, and without defect_id if asked."""
    columns = list(COLUMNS)
    if not any(row['transformation'] for row in rows):
        columns.remove('transformation')
        columns.remove('expected_value')
    if not with_defects:
        columns.remove('defect_id')
    return columns


def comparison_csv(rows, columns=None):
    """CSV text of the rows; no cell will run as a formula in a spreadsheet app."""
    columns = columns or comparison_columns(rows)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_safe(row.get(column, '')) for column in columns})
    return buffer.getvalue()
