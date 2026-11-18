from decimal import Decimal, InvalidOperation
from datetime import date

from .transforms import TransformError, apply_rule, plain

# Text accepted for a boolean field (case-insensitive), so a SQL bit (1/0) or a Y/N flag in Source
# compares equal to a JSON true/false in Target.
TRUE_TEXT = {'true', 't', 'yes', 'y', '1'}
FALSE_TEXT = {'false', 'f', 'no', 'n', '0'}

# Result of one row in the row-by-row comparison (reconcile()['comparisons']).
MATCH = 'MATCH'
MISMATCH = 'MISMATCH'
MISSING_IN_TARGET = 'MISSING IN TARGET'
EXTRA_IN_TARGET = 'EXTRA IN TARGET'


def nested(record, path):
    value = record
    parts = path.split('.')
    for index, part in enumerate(parts):
        if isinstance(value, list):
            # A single-item array (e.g. ReceivableHeader: [{...}]) is read like an object.
            if len(value) != 1:
                # Several items cannot map to one flat value: return every item's value so the mismatch is visible.
                return [nested(item, '.'.join(parts[index:])) for item in value] or None
            value = value[0]
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def is_absent(record, path):
    """True when the record has no attribute at path; an empty array on the path holds none.

    nested() returns None both for an explicit null and for an absent attribute; this tells them apart.
    """
    value = record
    for part in path.split('.'):
        if isinstance(value, list):
            if len(value) != 1:
                return not value  # several items: nested() returns each item's value
            value = value[0]
        if not isinstance(value, dict) or part not in value:
            return True
        value = value[part]
    return False


def normalize(value, kind):
    if value is None:
        return None
    if kind == 'decimal':
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return ('INVALID_DECIMAL', str(value))
        # NaN/Infinity are not amounts, and NaN never equals itself, so treat them as unparseable.
        return number if number.is_finite() else ('INVALID_DECIMAL', str(value))
    if kind == 'date':
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return ('INVALID_DATE', str(value))
    if kind == 'boolean':
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float, Decimal)):
            # Only the numbers 1 and 0 are flags; any other number (or NaN) is not a boolean.
            return True if value == 1 else False if value == 0 else ('INVALID_BOOLEAN', str(value))
        text = str(value).strip().lower()
        if text in TRUE_TEXT:
            return True
        if text in FALSE_TEXT:
            return False
        return ('INVALID_BOOLEAN', str(value))
    return str(value)


def reconcile(source, target, mapping):
    sk, tk = mapping['key']['source'], mapping['key']['target']
    def index(rows, path):
        indexed, duplicates = {}, []
        for row in rows:
            key = nested(row, path)
            if key is None:
                duplicates.append({'key': None, 'reason': 'missing business key'})
            elif isinstance(key, list):
                duplicates.append({'key': None, 'reason': 'business key has multiple values (array with several items)'})
            elif str(key) in indexed:
                duplicates.append({'key': str(key), 'reason': 'duplicate business key'})
            else:
                indexed[str(key)] = row
        return indexed, duplicates
    si, sd = index(source, sk)
    ti, td = index(target, tk)
    mismatches = []
    passed = []
    # Row-by-row comparison: every mapped field of every record, Source next to Target.
    comparisons = []
    for key in sorted(si.keys() | ti.keys()):
        if key not in ti or key not in si:
            comparisons.extend(_one_side_rows(key, si.get(key), ti.get(key), mapping))
            continue
        differences = []
        for field in mapping['fields']:
            sv = nested(si[key], field['source'])
            tv = nested(ti[key], field['target'])
            difference = {'source_field': field['source'], 'target_field': field['target'], 'source_value': sv, 'target_value': tv}
            # Both give None above; an explicit null and an absent attribute are still different.
            source_absent = sv is None and is_absent(si[key], field['source'])
            target_absent = tv is None and is_absent(ti[key], field['target'])
            if source_absent:
                difference['source_absent'] = True
            if target_absent:
                difference['target_absent'] = True
            rule = field.get('transformation')
            if not rule:
                differs = (normalize(sv, field['type']) != normalize(tv, field['type'])
                           or (sv is None and tv is None and source_absent != target_absent))
                if differs:
                    differences.append(difference)
                comparisons.append({'key': key, **difference, 'result': MISMATCH if differs else MATCH})
                continue
            # Target should hold the STTM transformation of the Source value, not the Source value itself.
            difference['transformation'] = rule
            try:
                expected = apply_rule(rule, sv, si[key], field['source'])
            except TransformError as exc:
                difference['transformation_error'] = str(exc)
                differences.append(difference)
                comparisons.append({'key': key, **difference, 'result': MISMATCH})
                continue
            # A rule that leaves an absent Source attribute as nothing leaves it absent, not null.
            expected_absent = expected is None and source_absent
            differs = (normalize(expected, field['type']) != normalize(tv, field['type'])
                       or (expected is None and tv is None and expected_absent != target_absent))
            if differs:
                difference['expected_value'] = plain(expected)
                differences.append(difference)
            comparisons.append({'key': key, **difference, 'expected_value': plain(expected),
                                'result': MISMATCH if differs else MATCH})
        if differences:
            mismatches.append({'key': key, 'differences': differences})
        else:
            passed.append(key)
    return {'summary': {'source_count': len(source), 'target_count': len(target), 'passed': len(passed), 'mismatched': len(mismatches), 'missing_in_target': len(si.keys()-ti.keys()), 'extra_in_target': len(ti.keys()-si.keys()), 'key_issues': len(sd)+len(td)}, 'passed_keys': passed, 'mismatches': mismatches, 'missing_in_target': sorted(si.keys()-ti.keys()), 'extra_in_target': sorted(ti.keys()-si.keys()), 'key_issues': {'source': sd, 'target': td}, 'comparisons': comparisons}


def _one_side_rows(key, source_row, target_row, mapping):
    """Comparison rows for a record found on one side only: its mapped values, nothing to compare them with."""
    rows = []
    for field in mapping['fields']:
        row = {'key': key, 'source_field': field['source'], 'target_field': field['target']}
        if source_row is not None:
            row['source_value'] = nested(source_row, field['source'])
            if row['source_value'] is None and is_absent(source_row, field['source']):
                row['source_absent'] = True
            if field.get('transformation'):
                row['transformation'] = field['transformation']
        else:
            row['target_value'] = nested(target_row, field['target'])
            if row['target_value'] is None and is_absent(target_row, field['target']):
                row['target_absent'] = True
        row['result'] = MISSING_IN_TARGET if target_row is None else EXTRA_IN_TARGET
        rows.append(row)
    return rows
