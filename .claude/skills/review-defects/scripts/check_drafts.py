"""Check defect drafts against the db-validation-agent reporting rules.

Run from the project root with the project interpreter:

  .venv/Scripts/python.exe .claude/skills/review-defects/scripts/check_drafts.py
      Generate drafts from the bundled sample data and built-in edge cases, then check them.

  .venv/Scripts/python.exe .claude/skills/review-defects/scripts/check_drafts.py --report <defect_drafts.json>
          [--source-name "<name>"] [--target-name "<name>"]
          [--source-file <source.json> --target-file <target.json> --mapping <STTM file>]
      Check a saved report: reports/defect_drafts.json from `python -m src.main`, or the file the
      Streamlit UI saves with "Download defect drafts (JSON)". Each draft is also compared with the
      records it was generated from: the files given with --source-file/--target-file/--mapping, or
      else the files named in reports/last_run.json when that run wrote this report.

Exit code: 0 every rule holds, 1 at least one rule is violated, 2 the check could not run.
The rules are written out here, independently of src/defect_generator.py, so a change to the
generator cannot quietly change what is being checked.
"""
import argparse
import csv
import io
import json
import re
import sys
import traceback
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if not (ROOT / 'src' / 'defect_generator.py').is_file():
    sys.exit(f'Cannot find src/defect_generator.py under {ROOT}. Keep this script in .claude/skills/review-defects/scripts/.')
sys.path.insert(0, str(ROOT))

from src.defect_generator import build_defect_report, defects_to_csv  # noqa: E402
from src.sttm import parse_sttm  # noqa: E402
from src.transforms import TransformError, apply_rule  # noqa: E402
from src.validator import reconcile  # noqa: E402

DRAFT_STATUS = 'Draft - Pending Review'
ABSENT = '<attribute absent>'
TRANSFORM_FAILED = '<transformation failed>'
EXTRA = 'EXTRA_RECORD_IN_TARGET'
MISSING_IN_TARGET = 'MISSING_RECORD_IN_TARGET'
DUPLICATE = 'DUPLICATE_BUSINESS_KEY'
FIELD_VALUE = 'FIELD_VALUE_MISMATCH'
FIELD_CATEGORIES = {FIELD_VALUE, 'NULL_VALUE_MISMATCH', 'DATA_TYPE_MISMATCH'}
CATEGORIES = FIELD_CATEGORIES | {MISSING_IN_TARGET, EXTRA, DUPLICATE}
REQUIRED = ['defect_id', 'defect_title', 'severity', 'record_identifier', 'business_key', 'issue_category',
            'affected_field', 'expected_value', 'actual_value', 'defect_description', 'preconditions',
            'steps_to_reproduce', 'expected_result', 'actual_result', 'probable_cause', 'business_impact', 'status']
MAY_BE_NULL = {'expected_value', 'actual_value'}
TEXT_FIELDS = ('defect_title', 'defect_description', 'steps_to_reproduce', 'expected_result', 'actual_result',
               'probable_cause', 'business_impact')
FORBIDDEN_WORDS = ('SQL Server', 'Cosmos', 'COSMOS')
FIELD_RE = re.compile(r'^(.*) \(Source\) -> (.*) \(Target\)$')
DUPLICATE_SIDE_RE = re.compile(r'\((Source|Target) business key\)$')
PATH_RE = re.compile(r'\.(json|csv|xlsx)\b|[A-Za-z]:\\', re.IGNORECASE)
FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')
PLAIN_NUMBER = re.compile(r'[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?')
MISSING, MULTI = object(), object()


def lookup(record, path):
    """Value at a dotted path; MISSING when absent, MULTI when an array with several items is on the path."""
    value = record
    for part in path.split('.'):
        if isinstance(value, list):
            if not value:
                return MISSING
            if len(value) > 1:
                return MULTI
            value = value[0]
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def first_rows(rows, key_path):
    indexed = {}
    for row in rows or []:
        key = lookup(row, key_path) if isinstance(row, dict) else MISSING
        if key is not MISSING and key is not MULTI and key is not None:
            indexed.setdefault(str(key), row)
    return indexed


def key_counts(rows, key_path):
    counts = {}
    for row in rows or []:
        key = lookup(row, key_path) if isinstance(row, dict) else MISSING
        if key is not MISSING and key is not MULTI and key is not None:
            counts[str(key)] = counts.get(str(key), 0) + 1
    return counts


def load_records(path, label):
    records = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ValueError(f'{label} file {path} must be a JSON array of objects')
    return records


def files_of_run(report_path):
    """Return ({'source', 'target', 'mapping'}, note) for the run that wrote report_path, or (None, reason).

    The files come from reports/last_run.json. src.main writes the drafts and then last_run.json,
    so a report newer than last_run.json was written by something else and is not paired with it.
    """
    run_path = ROOT / 'reports' / 'last_run.json'
    try:
        run = json.loads(run_path.read_text(encoding='utf-8-sig'))
        same = Path(report_path).samefile(run['defects_report'])
        run_time = run_path.stat().st_mtime
        rewritten = Path(report_path).stat().st_mtime > run_time + 2
    except (OSError, ValueError, KeyError, TypeError):
        return None, 'reports/last_run.json does not name this report'
    if not same:
        return None, 'reports/last_run.json names a different report'
    if rewritten:
        return None, 'the report was rewritten after the run recorded in reports/last_run.json'
    files = {name: run.get(f'{name}_file') for name in ('source', 'target', 'mapping')}
    if not all(files.values()):
        return None, 'reports/last_run.json does not name the source, target and mapping files'
    changed = [path for path in files.values() if Path(path).is_file() and Path(path).stat().st_mtime > run_time]
    note = f"changed after the run, so a difference may come from that edit: {', '.join(changed)}" if changed else None
    return files, note


def transformed(rule, record, path):
    """The value Target should hold for a field with an STTM transformation, as a draft shows it."""
    raw = lookup(record, path)
    try:
        value = apply_rule(rule, None if raw is MISSING else raw, record, path)
    except TransformError:
        return TRANSFORM_FAILED
    if value is None and raw is MISSING:
        return ABSENT
    return str(value) if isinstance(value, Decimal) else value


def text_of(draft, fields):
    parts = []
    for name in fields:
        value = draft.get(name)
        parts.extend(value if isinstance(value, list) else [value])
    return '\n'.join(str(p) for p in parts if p is not None)


def check_report(report, expect, result=None, source=None, target=None, mapping=None):
    """Return [(rule, where, message)].

    expect maps 'Source'/'Target' to the exact DB name, '' when the drafts must use the generic
    wording, or None when the name is unknown (then only consistent naming is required).
    """
    found = []

    def bad(rule, where, message):
        found.append((rule, where, message))

    status = report.get('validation_status')
    defects = report.get('defects') or []
    reasons = report.get('incomplete_reasons') or []
    anomalies = report.get('anomalies') or []

    if status not in ('PASS', 'FAIL', 'INCOMPLETE'):
        bad('R-STATUS', 'report', f'unknown validation_status {status!r}')
    elif status == 'PASS' and (defects or reasons):
        bad('R-STATUS', 'report', f'PASS with {len(defects)} draft(s) and {len(reasons)} incomplete reason(s)')
    elif status == 'FAIL' and (not defects or reasons):
        bad('R-STATUS', 'report', 'FAIL must have at least one draft and no incomplete reasons')
    elif status == 'INCOMPLETE' and not reasons:
        bad('R-STATUS', 'report', 'INCOMPLETE without any incomplete_reasons')

    key_problems = any(d.get('issue_category') == 'DUPLICATE_BUSINESS_KEY' for d in defects)
    if result is not None:
        key_problems = key_problems or any((result.get('key_issues') or {}).values())
    if key_problems and status != 'INCOMPLETE':
        bad('R-INCOMPLETE', 'report', f'duplicate or missing business keys exist but status is {status}')

    extra_keys = [str(d.get('business_key')) for d in defects if d.get('issue_category') == EXTRA]
    anomaly_text = '\n'.join(map(str, anomalies))
    for key in extra_keys:
        if key not in anomaly_text:
            bad('R-CRITICAL', key, 'extra-in-target record is not listed in anomalies')
    if anomalies and not extra_keys:
        bad('R-CRITICAL', 'report', 'anomalies reported without any extra-in-target draft')

    ids = [d.get('defect_id') for d in defects]
    for defect_id in sorted({i for i in ids if ids.count(i) > 1}, key=str):
        bad('R-UNIQUE', defect_id, 'defect_id is used more than once')
    seen = set()
    for d in defects:
        signature = (d.get('issue_category'), str(d.get('business_key')), d.get('affected_field'))
        if signature in seen:
            bad('R-UNIQUE', d.get('defect_id'), f'duplicate draft for {signature}')
        seen.add(signature)

    for side in ('Source', 'Target'):
        labels = set()
        for d in defects:
            text = text_of(d, TEXT_FIELDS + ('preconditions',))
            labels |= {m.group(1) for m in re.finditer(rf"{side} DB(?: '([^']*)')?", text)}
        shown = sorted('generic' if label is None else repr(label) for label in labels)
        want = expect.get(side)
        if want is None:
            if len(labels) > 1:
                bad('R-DBNAMES', 'report', f'{side} DB is named inconsistently: {shown}')
        elif labels - {want or None}:
            bad('R-DBNAMES', 'report', f"{side} DB should appear only as {repr(want) if want else 'generic'}; found {shown}")

    source_rows = target_rows = None
    rules = {(f.get('source'), f.get('target')): f.get('transformation') for f in (mapping or {}).get('fields', [])}
    if mapping and source is not None and target is not None:
        source_rows = first_rows(source, mapping['key']['source'])
        target_rows = first_rows(target, mapping['key']['target'])
        counts = {'Source': key_counts(source, mapping['key']['source']),
                  'Target': key_counts(target, mapping['key']['target'])}

    def present_in(key):
        sides = [side for side, rows in (('Source', source_rows), ('Target', target_rows)) if key in rows]
        return ' and '.join(sides) + ('' if len(sides) == 2 else ' only') if sides else 'neither Source nor Target'

    for d in defects:
        did = d.get('defect_id', '?')
        category = d.get('issue_category')
        missing = [f for f in REQUIRED if f not in d or (f not in MAY_BE_NULL and d[f] in ('', [], None))]
        if missing:
            bad('R-FIELDS', did, f"missing or empty: {', '.join(missing)}")
        if d.get('status') != DRAFT_STATUS:
            bad('R-DRAFT', did, f"status is {d.get('status')!r}, expected {DRAFT_STATUS!r}")
        if category not in CATEGORIES:
            bad('R-CATEGORY', did, f'unknown issue_category {category!r}')
        if not str(d.get('probable_cause', '')).startswith('Hypothesis (unverified):'):
            bad('R-HYPOTHESIS', did, 'probable cause is not stated as an unverified hypothesis')
        critical = d.get('severity') == 'Critical'
        if category == EXTRA and not (critical and str(d.get('defect_title', '')).startswith('[CRITICAL]')):
            bad('R-CRITICAL', did, 'extra-in-target draft must be severity Critical with a [CRITICAL] title')
        if category != EXTRA and critical:
            bad('R-CRITICAL', did, f'{category} must not be Critical')
        text = text_of(d, TEXT_FIELDS)
        for word in FORBIDDEN_WORDS:
            if word in text:
                bad('R-WORDING', did, f'uses {word!r}; drafts must say Source and Target')
                break
        if PATH_RE.search(text_of(d, ('steps_to_reproduce',))):
            bad('R-PATHS', did, 'steps to reproduce contain a file path')
        impact = str(d.get('business_impact', ''))
        if category in FIELD_CATEGORIES and not (
                impact.startswith('Not determined') or (category == FIELD_VALUE and impact.startswith('Numeric variance observed'))):
            bad('R-IMPACT', did, f'business impact is not backed by the reconciliation result: {impact[:90]!r}')

        key = str(d.get('business_key'))
        if source_rows is not None and category in (MISSING_IN_TARGET, EXTRA):
            claimed = 'Source only' if category == MISSING_IN_TARGET else 'Target only'
            if present_in(key) != claimed:
                bad('R-VALUES', did, f'says {key!r} is in {claimed}, but the records have it in {present_in(key)}')
        if source_rows is not None and category == DUPLICATE:
            side = DUPLICATE_SIDE_RE.search(str(d.get('affected_field', '')))
            if side and d.get('actual_value') != counts[side.group(1)].get(key, 0):
                bad('R-VALUES', did, f"says {key!r} appears {d.get('actual_value')!r} times in {side.group(1)}, "
                                     f"but the records hold it {counts[side.group(1)].get(key, 0)} time(s)")

        match = FIELD_RE.match(str(d.get('affected_field', '')))
        if category not in FIELD_CATEGORIES or not match:
            continue
        sf, tf = match.groups()
        rule = d.get('transformation')
        # With an STTM transformation, expected_value is the transformed value and source_value the raw one.
        source_shown = d.get('source_value') if rule else d.get('expected_value')
        if rule and 'source_value' not in d:
            bad('R-FIELDS', did, 'a draft with a transformation must give the raw source_value')
        if mapping and (sf, tf) in rules and rules[(sf, tf)] != rule:
            bad('R-VALUES', did, f'draft applies transformation {rule!r} but the STTM gives {rules[(sf, tf)]!r} for this field')
        # Two explicit nulls are identical too; an explicit null vs an absent attribute shows as None vs ABSENT.
        if 'expected_value' in d and 'actual_value' in d and d['expected_value'] == d['actual_value']:
            bad('R-VALUES', did, f"Source and Target hold the same value {d.get('expected_value')!r} but a mismatch was reported")
        actual_result = str(d.get('actual_result', ''))
        for side, path, value, end in (('Target', tf, d.get('actual_value'), ';'), ('Source', sf, source_shown, '.')):
            says_absent = f"{side} '{path}' is {ABSENT}{end}" in actual_result
            says_null = f"{side} '{path}' is NULL{end}" in actual_result
            if says_absent != (value == ABSENT) or says_null != (value is None):
                said = 'absent' if says_absent else 'NULL' if says_null else 'a value'
                bad('R-NULL-ABSENT', did, f'actual result says {side} is {said} but the value column holds {value!r}')
        if source_rows is None:
            continue
        checks = [('source_value' if rule else 'expected_value', source_rows, sf, source_shown),
                  ('actual_value', target_rows, tf, d.get('actual_value'))]
        if rule:
            checks.append(('expected_value', source_rows, sf, d.get('expected_value')))
        for column, rows, path, value in checks:
            if key not in rows:
                bad('R-VALUES', did, f'{column}: no record with key {key!r} in the data')
                continue
            raw = lookup(rows[key], path)
            if raw is MULTI:
                continue
            if rule and column == 'expected_value':
                want = transformed(rule, rows[key], path)
                if value != want:
                    bad('R-VALUES', did, f'expected_value is {value!r} but the STTM transformation {rule!r} '
                                         f'gives {want!r} for this record')
                continue
            want = ABSENT if raw is MISSING else raw
            if value != want:
                bad('R-VALUES', did, f'{column} is {value!r} but the record holds {want!r}')

    try:
        rows = list(csv.DictReader(io.StringIO(defects_to_csv(defects))))
    except Exception as exc:  # the export itself failing is a finding
        bad('R-CRASH', 'CSV export', f'{type(exc).__name__}: {exc}')
        rows = []
    for number, row in enumerate(rows, start=1):
        for column, cell in row.items():
            if cell and cell.startswith(FORMULA_PREFIXES) and not PLAIN_NUMBER.fullmatch(cell):
                bad('R-CSV', row.get('defect_id') or f'row {number}', f'column {column!r} would run as a formula in Excel: {cell[:60]!r}')
    return found


# ---- built-in scenarios -------------------------------------------------------------------------

EDGE_MAPPING = {
    'key': {'source': 'InvoiceNumber', 'target': 'invoice.number'},
    'fields': [
        {'source': 'Voucher', 'target': 'voucher', 'type': 'string'},
        {'source': 'Amount', 'target': 'payment.amount', 'type': 'decimal'},
        {'source': 'ApprovalStatus', 'target': 'status', 'type': 'string'},
        {'source': 'DueDate', 'target': 'payment.dueDate', 'type': 'date'},
        {'source': 'Memo', 'target': 'header.memo', 'type': 'string'},
    ],
}


def src(key, drop=(), **fields):
    row = {'InvoiceNumber': key, 'Voucher': f'V-{key}', 'Amount': '100.00', 'ApprovalStatus': 'Approved',
           'DueDate': '2026-11-10', 'Memo': 'm'}
    row.update(fields)
    for name in drop:
        row.pop(name, None)
    return row


def tgt(key, drop=(), **fields):
    row = {'invoice': {'number': key}, 'voucher': f'V-{key}', 'payment': {'amount': 100, 'dueDate': '2026-11-10'},
           'status': 'Approved', 'header': {'memo': 'm'}}
    for name, value in fields.items():
        if name in ('amount', 'dueDate'):
            row['payment'][name] = value
        else:
            row[name] = value
    for name in drop:
        row.pop(name, None)
    return row


EDGE_SOURCE = [
    src('E01', Amount='1500.00'),                 # decimal mismatch
    src('E02'),                                   # string mismatch
    src('E03'),                                   # Target explicit null
    src('E04'),                                   # Target attribute absent
    src('E05'),                                   # Target field inside an empty array
    src('E06'),                                   # Target unparseable decimal
    src('E07', Amount='Infinity'),                # non-finite decimal
    src('E08', Amount='NaN'),                     # NaN on both sides: not a mismatch
    src('E09', Amount='-9E+999999'),              # extreme exponent
    src('E10', Voucher='-2+3', Memo='@SUM(1)'),   # formula-like values
    src('=1+1', ApprovalStatus='A'),              # formula-like business key
    src('E12'),                                   # missing in Target
    src('E13', Memo=None),                        # Source explicit null, Target attribute absent
    src('E14', drop=('Memo',)),                   # Source attribute absent, Target explicit null
    src('E15', Memo=None),                        # explicit null on both sides: not a mismatch
    src('E16', drop=('Memo',)),                   # absent on both sides: not a mismatch
]
EDGE_TARGET = [
    tgt('E01', amount=1400),
    tgt('E02', status='Pending'),
    tgt('E03', status=None),
    tgt('E04', drop=('status',)),
    tgt('E05', header=[]),
    tgt('E06', amount='abc'),
    tgt('E07'),
    tgt('E08', amount='NaN'),
    tgt('E09', amount='9E+999999'),
    tgt('E10', voucher='-2+3', status='=HYPERLINK("http://example.invalid","x")'),
    tgt('=1+1', status='B'),
    tgt('E11'),                                   # extra in Target
    tgt('E13', header={}),
    tgt('E14', header={'memo': None}),
    tgt('E15', header={'memo': None}),
    tgt('E16', header={}),
]
BOOL_MAPPING = {'key': {'source': 'Id', 'target': 'id'},
                'fields': [{'source': 'IsActive', 'target': 'metadata.enabled', 'type': 'boolean'}]}
BOOL_SOURCE = [{'Id': 'B01', 'IsActive': 1},        # SQL bit vs JSON true: not a mismatch
               {'Id': 'B02', 'IsActive': 'Y'},      # Y vs false: value mismatch
               {'Id': 'B03', 'IsActive': 'maybe'},  # unparseable boolean
               {'Id': 'B04', 'IsActive': None}]     # null vs false
BOOL_TARGET = [{'id': 'B01', 'metadata': {'enabled': True}}, {'id': 'B02', 'metadata': {'enabled': False}},
               {'id': 'B03', 'metadata': {'enabled': True}}, {'id': 'B04', 'metadata': {'enabled': False}}]
TRANSFORM_MAPPING = {'key': {'source': 'Id', 'target': 'id'}, 'fields': [
    {'source': 'Name.First', 'target': 'fullName', 'type': 'string', 'transformation': "concat(First, ' ', Last) | upper"},
    {'source': 'Status', 'target': 'status', 'type': 'string', 'transformation': 'map(A=Active; I=Inactive; *=Unknown)'},
    {'source': 'Amount', 'target': 'amountCents', 'type': 'decimal', 'transformation': 'multiply(100) | round(0)'},
    {'source': 'Opened', 'target': 'openedOn', 'type': 'date', 'transformation': 'to_date(DD/MM/YYYY)'},
    {'source': 'Region', 'target': 'region', 'type': 'string', 'transformation': "trim | default('N/A')"},
    {'source': 'Code', 'target': 'code', 'type': 'string', 'transformation': 'upper'},
]}
TRANSFORM_SOURCE = [
    {'Id': 'T01', 'Name': {'First': 'ann', 'Last': 'lee'}, 'Status': 'A', 'Amount': '12.34',   # every rule holds
     'Opened': '05/11/2026', 'Region': ' EU ', 'Code': 'ab'},
    {'Id': 'T02', 'Name': {'First': 'bob', 'Last': 'ray'}, 'Status': 'I', 'Amount': '1.5',     # mapped code and amount
     'Opened': '31/12/2026', 'Region': None},                                                  # differ; null -> 'N/A';
    {'Id': 'T03', 'Name': {'First': 'cy', 'Last': None}, 'Status': 'X', 'Amount': 'abc',      # absent Code stays absent
     'Opened': '2026-11-05', 'Code': 'x'},                                                     # rules fail on 'abc' and
                                                                                               # an ISO date
    {'Id': 'T04', 'Name': {'First': 'ann', 'Last': 'lee'}, 'Status': 'A', 'Amount': '12.34',   # null Code stays null,
     'Opened': '05/11/2026', 'Region': ' EU ', 'Code': None},                                  # Target code absent
    {'Id': 'T05', 'Name': {'First': 'ann', 'Last': 'lee'}, 'Status': 'A', 'Amount': '12.34',   # absent Code stays absent,
     'Opened': '05/11/2026', 'Region': ' EU '},                                                # Target code null
]
TRANSFORM_TARGET = [
    {'id': 'T01', 'fullName': 'ANN LEE', 'status': 'Active', 'amountCents': 1234, 'openedOn': '2026-11-05',
     'region': 'EU', 'code': 'AB'},
    {'id': 'T02', 'fullName': 'BOB RAY', 'status': 'Active', 'amountCents': 140, 'openedOn': '2026-12-31',
     'region': None, 'code': 'ZZ'},
    {'id': 'T03', 'fullName': 'CY ', 'status': 'Unknown', 'amountCents': 0, 'openedOn': '2026-11-05', 'code': 'X'},
    {'id': 'T04', 'fullName': 'ANN LEE', 'status': 'Active', 'amountCents': 1234, 'openedOn': '2026-11-05',
     'region': 'EU'},
    {'id': 'T05', 'fullName': 'ANN LEE', 'status': 'Active', 'amountCents': 1234, 'openedOn': '2026-11-05',
     'region': 'EU', 'code': None},
]
NAMES = {'Source': 'Finance ERP (UAT)', 'Target': 'recv-db_01'}
NO_NAMES = {'Source': '', 'Target': ''}


def scenarios():
    yield 'edge cases, with DB names', EDGE_SOURCE, EDGE_TARGET, EDGE_MAPPING, NAMES
    yield 'edge cases, no DB names', EDGE_SOURCE, EDGE_TARGET, EDGE_MAPPING, NO_NAMES
    yield ('duplicate business key', [src('D01'), src('D01', Amount='999.00'), src('D02', Amount='5.00')],
           [tgt('D01'), tgt('D02')], EDGE_MAPPING, NAMES)
    yield ('missing business key', [src('K01'), {'Voucher': 'V-x'}], [tgt('K01'), tgt('K02')], EDGE_MAPPING, NAMES)
    yield 'boolean fields, with DB names', BOOL_SOURCE, BOOL_TARGET, BOOL_MAPPING, NAMES
    yield 'boolean fields, no DB names', BOOL_SOURCE, BOOL_TARGET, BOOL_MAPPING, NO_NAMES
    yield 'STTM transformations, with DB names', TRANSFORM_SOURCE, TRANSFORM_TARGET, TRANSFORM_MAPPING, NAMES
    yield 'STTM transformations, no DB names', TRANSFORM_SOURCE, TRANSFORM_TARGET, TRANSFORM_MAPPING, NO_NAMES
    try:
        mapping = parse_sttm((ROOT / 'config' / 'mapping.json').read_bytes(), 'config/mapping.json')
        source = json.loads((ROOT / 'data' / 'sql_sample.json').read_text(encoding='utf-8-sig'))
        target = json.loads((ROOT / 'data' / 'cosmos_sample.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        yield f'bundled sample (could not load: {exc})', None, None, None, NAMES
        return
    yield 'bundled sample + config/mapping.json, with DB names', source, target, mapping, {
        'Source': 'FinanceERP', 'Target': 'receivables-db'}


def run_scenario(title, source, target, mapping, names):
    if mapping is None:
        return title, None, [('R-CRASH', title, 'inputs could not be loaded')], None
    context = {'source_dataset': f'{title} (source)', 'target_dataset': f'{title} (target)', 'mapping_file': 'scenario mapping',
               'source_name': names['Source'] or None, 'target_name': names['Target'] or None}
    try:
        result = reconcile(source, target, mapping)
        report = build_defect_report(result, mapping, source, target, context)
    except Exception:
        return title, None, [('R-CRASH', title, traceback.format_exc().strip().splitlines()[-1])], None
    return title, report, check_report(report, names, result, source, target, mapping), None


def check_saved_report(args, report):
    """Check a saved report, comparing it with the records it came from when they can be found."""
    given = [args.source_file, args.target_file, args.mapping]
    if any(given) and not all(given):
        raise ValueError('give all of --source-file, --target-file and --mapping, or none of them')
    files, note = (dict(zip(('source', 'target', 'mapping'), given)), None) if all(given) else files_of_run(args.report)
    source = target = mapping = result = None
    if files:
        try:
            mapping = parse_sttm(Path(files['mapping']).read_bytes(), files['mapping'])
            source = load_records(files['source'], 'Source')
            target = load_records(files['target'], 'Target')
            result = reconcile(source, target, mapping)
        except (OSError, ValueError, KeyError) as exc:
            if all(given):
                raise ValueError(f'cannot load the records to compare with: {exc}') from exc
            files, note = None, f'the files named in reports/last_run.json could not be loaded ({exc})'
            source = target = mapping = result = None
    if files:
        data = f"compared with the records in: {files['source']} | {files['target']} | {files['mapping']}"
        if note:
            data += f'\n   note: {note}'
    else:
        data = (f'values NOT compared with the records: {note}. '
                'Pass --source-file, --target-file and --mapping to compare them.')
    names = {'Source': args.source_name, 'Target': args.target_name}
    return f'report {args.report}', report, check_report(report, names, result, source, target, mapping), data


def main():
    parser = argparse.ArgumentParser(description='Check defect drafts against the db-validation-agent rules.')
    parser.add_argument('--report', help='saved defect_drafts.json to check instead of generating drafts')
    parser.add_argument('--source-name', help="exact Source DB name the drafts should use ('' = generic wording)")
    parser.add_argument('--target-name', help="exact Target DB name the drafts should use ('' = generic wording)")
    parser.add_argument('--source-file', help='with --report: Source JSON the drafts were generated from')
    parser.add_argument('--target-file', help='with --report: Target JSON the drafts were generated from')
    parser.add_argument('--mapping', help='with --report: STTM mapping (.json, .csv or .xlsx) the drafts were generated from')
    args = parser.parse_args()

    if args.report:
        try:
            report = json.loads(Path(args.report).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            print(f'Cannot read {args.report}: {exc}')
            return 2
        if not isinstance(report, dict) or not isinstance(report.get('defects'), list):
            print(f'{args.report} is not a defect report (expected an object with validation_status and defects).')
            return 2
        try:
            runs = [check_saved_report(args, report)]
        except ValueError as exc:
            print(f'Cannot check {args.report}: {exc}')
            return 2
    else:
        runs = [run_scenario(*scenario) for scenario in scenarios()]

    total_drafts = total_violations = 0
    for title, report, violations, data in runs:
        drafts = len(report.get('defects', [])) if report else 0
        total_drafts += drafts
        total_violations += len(violations)
        status = report.get('validation_status') if report else 'not generated'
        print(f'== {title}: status {status}, {drafts} draft(s)')
        if data:
            print(f'   {data}')
        for rule, where, message in violations:
            print(f'   [{rule}] {where}: {message}')
        if not violations:
            print('   all rules hold')
    print(f'\n{len(runs)} scenario(s), {total_drafts} draft(s) checked, {total_violations} violation(s).')
    return 1 if total_violations else 0


if __name__ == '__main__':
    sys.exit(main())
