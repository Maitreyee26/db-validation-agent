"""Builds defect drafts from the output of src.validator.reconcile.

Every value placed in a draft comes from the reconciliation result or from the
records that were reconciled. Nothing is fetched from a database and nothing is
submitted to a defect tracker: drafts are left in "Draft - Pending Review".
"""
import csv
import io
import re
from decimal import MAX_EMAX, MIN_EMIN, Decimal, localcontext

from .validator import nested, normalize

STATUS = 'Draft - Pending Review'

FIELD_VALUE_MISMATCH = 'FIELD_VALUE_MISMATCH'
MISSING_IN_TARGET = 'MISSING_RECORD_IN_TARGET'
EXTRA_IN_TARGET = 'EXTRA_RECORD_IN_TARGET'
DUPLICATE_BUSINESS_KEY = 'DUPLICATE_BUSINESS_KEY'
NULL_VALUE_MISMATCH = 'NULL_VALUE_MISMATCH'
DATA_TYPE_MISMATCH = 'DATA_TYPE_MISMATCH'

CATEGORY_LABELS = {
    FIELD_VALUE_MISMATCH: 'Field Value Mismatch',
    MISSING_IN_TARGET: 'Missing Record in Target',
    EXTRA_IN_TARGET: 'Extra Record in Target',
    DUPLICATE_BUSINESS_KEY: 'Duplicate Business Key',
    NULL_VALUE_MISMATCH: 'Null Value Mismatch',
    DATA_TYPE_MISMATCH: 'Data Type Mismatch',
}

PROBABLE_CAUSES = {
    FIELD_VALUE_MISMATCH: 'Possible transformation, rounding, or mapping logic difference in the migration/sync process.',
    MISSING_IN_TARGET: 'Possible migration filter, failed write, or incomplete load into the target.',
    EXTRA_IN_TARGET: ('Target data is expected to be derived from source, so a target-only key is unexpected. '
                      'Possible causes: source and target extracts cover a different scope or environment, '
                      'the source record was deleted after migration, or the record was loaded into the target '
                      'outside the migration process.'),
    DUPLICATE_BUSINESS_KEY: 'Possible missing uniqueness constraint or a repeated load producing more than one record for the key.',
    NULL_VALUE_MISMATCH: 'Possible unmapped attribute, default-value handling, or null propagation difference in the migration/sync process.',
    DATA_TYPE_MISMATCH: 'Possible serialization or type-conversion difference between Source and Target.',
}

# Probable causes for a field that has an STTM transformation.
TRANSFORM_MISMATCH_CAUSE = ('Possible missing or incorrect implementation of the STTM transformation in the '
                            'migration/sync process, or Source data the transformation rule does not cover.')
TRANSFORM_FAILED_CAUSE = ('Possible unexpected Source data, or the STTM transformation does not match the format '
                          'of the Source data.')
# Probable cause when one side holds an explicit null and the other has no attribute at all.
NULL_ABSENT_CAUSE = ('Possible serialization difference in the migration/sync process: null attributes may be '
                     'dropped from the document, or absent attributes written as null, instead of being kept as '
                     'they are in Source.')

NOT_DETERMINED = 'Not determined - insufficient evidence in the reconciliation result. To be assessed during review.'

# The only precondition on every draft: testers reproduce the defect from the Azure portal.
PRECONDITIONS = ['Log in to the Azure portal.']

# Target is always built from source, so a target-only key is treated as a critical anomaly.
SEVERITY = {EXTRA_IN_TARGET: 'Critical'}
DEFAULT_SEVERITY = 'To be assessed during review'

CSV_COLUMNS = [
    'defect_id', 'defect_title', 'severity', 'record_identifier', 'business_key', 'voucher', 'issue_category',
    'affected_field', 'expected_value', 'actual_value', 'defect_description', 'preconditions',
    'steps_to_reproduce', 'expected_result', 'actual_result', 'probable_cause', 'business_impact', 'status',
    # Filled only for a field with an STTM transformation: the raw Source value and the rule applied to it.
    'source_value', 'transformation',
]
# Columns that appear in the CSV only when a draft has them: voucher when the STTM maps a Voucher field,
# source_value and transformation when a field has an STTM transformation.
OPTIONAL_COLUMNS = {'voucher', 'source_value', 'transformation'}

_MISSING = object()
# Shown for an attribute the record does not have, in the text and in expected_value/actual_value,
# so it stays distinct from an explicit null (None in JSON, 'NULL' in CSV).
ABSENT = '<attribute absent>'
# expected_value when the STTM transformation cannot be applied to the Source value.
TRANSFORM_FAILED = '<transformation failed>'

# Largest exact Target - Source difference worth stating digit for digit; longer ones are rounded.
_MAX_EXACT_VARIANCE_DIGITS = 60

# A cell starting with one of these is run as a formula by spreadsheet apps (CSV injection).
_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')
_PLAIN_NUMBER = re.compile(r'[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?')


def _lookup(record, path):
    """Return the value at a dotted path, or _MISSING when the path is absent."""
    value = record
    parts = path.split('.')
    for index, part in enumerate(parts):
        if isinstance(value, list):
            if not value:
                return _MISSING  # an empty array holds no attribute at this path
            if len(value) != 1:
                return nested(value, '.'.join(parts[index:]))  # same multi-item handling as the validator
            value = value[0]
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _fmt(value):
    if value is _MISSING:
        return ABSENT
    if value is None:
        return 'NULL'
    return repr(value) if isinstance(value, str) else str(value)


def _plain(value):
    """JSON-friendly copy of an observed value."""
    if value is _MISSING:
        return ABSENT
    if isinstance(value, Decimal):
        return str(value)
    return value


def _decimal_variance(source_value, target_value):
    """Return (Target - Source as text, exact?), or (None, False) when it cannot be computed.

    Uses its own precision and exponent range: the default context rounds to 28
    digits and overflows on very large exponents.
    """
    sv, tv = normalize(source_value, 'decimal'), normalize(target_value, 'decimal')
    if not (isinstance(sv, Decimal) and isinstance(tv, Decimal) and sv.is_finite() and tv.is_finite()):
        return None, False
    digits = max(sv.adjusted(), tv.adjusted()) - min(sv.as_tuple().exponent, tv.as_tuple().exponent) + 2
    exact = digits <= _MAX_EXACT_VARIANCE_DIGITS
    try:
        with localcontext() as ctx:
            ctx.prec = digits if exact else 28
            ctx.Emax, ctx.Emin = MAX_EMAX, MIN_EMIN
            return str(tv - sv), exact
    except ArithmeticError:
        return None, False


def _csv_safe(value):
    """Cell text that spreadsheet apps will not run as a formula; plain numbers are left as they are."""
    text = '' if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES) and not _PLAIN_NUMBER.fullmatch(text):
        return "'" + text
    return text


def _is_invalid(normalized):
    return isinstance(normalized, tuple) and normalized and str(normalized[0]).startswith('INVALID_')


def _type_name(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float, Decimal)):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, list):
        return 'array'
    return type(value).__name__


def classify_difference(difference, field_type):
    """Map one reconcile() difference to a defect category.

    For a field with an STTM transformation, Target is compared with the transformed Source value.
    """
    if 'transformation_error' in difference:
        return DATA_TYPE_MISMATCH  # the Source value cannot be transformed, e.g. multiply(100) on 'abc'
    transformed = 'transformation' in difference
    sv = difference.get('expected_value') if transformed else difference.get('source_value')
    tv = difference.get('target_value')
    if (sv is None) != (tv is None):
        return NULL_VALUE_MISMATCH
    if sv is None and tv is None:
        return NULL_VALUE_MISMATCH  # reconcile() reports this only for an explicit null vs an absent attribute
    if field_type in ('decimal', 'date', 'boolean'):
        # A numeric string vs a number (or 1 vs true for a boolean) is normal; only unparseable values are type defects.
        if _is_invalid(normalize(sv, field_type)) or _is_invalid(normalize(tv, field_type)):
            return DATA_TYPE_MISMATCH
    else:
        def kind(value):
            # A transformation's numeric result is kept as text, so text and numbers are the same kind there.
            name = _type_name(value)
            return 'scalar' if transformed and name in ('string', 'number') else name
        if kind(sv) != kind(tv):
            return DATA_TYPE_MISMATCH
    return FIELD_VALUE_MISMATCH


def _index(rows, path):
    indexed = {}
    for row in rows or []:
        key = nested(row, path) if isinstance(row, dict) else None
        if key is not None:
            indexed.setdefault(str(key), []).append(row)
    return indexed


def _leaf(path):
    """Last segment of a dotted path: 'ReceivableHeader.InvoiceNumber' -> 'InvoiceNumber'."""
    return str(path).split('.')[-1]


def _voucher_paths(mapping):
    # Match on the last segment so nested fields such as 'ReceivableHeader.Voucher' are found too.
    for field in mapping.get('fields', []):
        if _leaf(field.get('source', '')).lower() == 'voucher':
            return field.get('source'), field.get('target')
    return None, None


def _check_completeness(result, source, target):
    """Return (structural, partial) reasons that make the reconciliation result unreliable.

    Structural reasons mean no defect can be trusted; partial reasons (key issues)
    affect only specific keys.
    """
    reasons, structural = [], []
    required = ('summary', 'passed_keys', 'mismatches', 'missing_in_target', 'extra_in_target', 'key_issues')
    absent = [name for name in required if name not in result]
    if absent:
        return [f"Reconciliation result is missing section(s): {', '.join(absent)}."], []
    summary = result['summary']
    key_issues = result.get('key_issues') or {}
    source_issues, target_issues = key_issues.get('source', []), key_issues.get('target', [])
    for issues, label in ((source_issues, 'Source'), (target_issues, 'Target')):
        duplicates = sorted({i['key'] for i in issues if i.get('reason') == 'duplicate business key'})
        no_key = sum(1 for i in issues if i.get('key') is None)
        if duplicates:
            reasons.append(f"Duplicate business key(s) in {label}: {', '.join(duplicates)}. "
                           'Only the first occurrence of each key was compared.')
        if no_key:
            reasons.append(f'{no_key} {label} record(s) have no usable business key (missing, or several values in an array) '
                           'and could not be reconciled.')
    matched = len(result['passed_keys']) + len(result['mismatches'])
    expected_source = matched + len(result['missing_in_target']) + len(source_issues)
    expected_target = matched + len(result['extra_in_target']) + len(target_issues)
    if summary.get('source_count') != expected_source or summary.get('target_count') != expected_target:
        structural.append('Record counts do not reconcile: '
                          f"Source {summary.get('source_count')} vs {expected_source} accounted for, "
                          f"Target {summary.get('target_count')} vs {expected_target} accounted for.")
    if source is not None and len(source) != summary.get('source_count'):
        structural.append(f"Source dataset has {len(source)} records but the result reports {summary.get('source_count')}.")
    if target is not None and len(target) != summary.get('target_count'):
        structural.append(f"Target dataset has {len(target)} records but the result reports {summary.get('target_count')}.")
    return structural, reasons


def _db_label(side, name):
    name = str(name or '').strip()
    return f"{side} DB '{name}'" if name else f'{side} DB'


class _Builder:
    def __init__(self, mapping, source, target, context):
        self.mapping = mapping or {}
        self.key = self.mapping.get('key', {})
        self.sk, self.tk = self.key.get('source', 'source key'), self.key.get('target', 'target key')
        # Records are named after the Source business key field, so the wording fits any payload.
        self.key_name = _leaf(self.sk)
        self.types = {(f.get('source'), f.get('target')): f.get('type', 'string') for f in self.mapping.get('fields', [])}
        self.voucher_source, self.voucher_target = _voucher_paths(self.mapping)
        self.source_rows = _index(source, self.sk) if source is not None else {}
        self.target_rows = _index(target, self.tk) if target is not None else {}
        self.context = dict(context or {})
        # Names entered at launch (e.g. "FinanceERP", "receivables-db"); generic wording when not given.
        self.source_db = _db_label('Source', self.context.get('source_name'))
        self.target_db = _db_label('Target', self.context.get('target_name'))
        self.defects = []

    def record(self, key, start=False):
        """Name a record by its business key, e.g. "the record with CustomerId 'C100'"."""
        return f"{'The' if start else 'the'} record with {self.key_name} {key!r}"

    def record_identifier(self, key, voucher):
        identifier = f'{self.key_name}: {key}'
        if self.voucher_source and self.voucher_source != self.sk:
            identifier += f' / Voucher: {voucher}'
        return identifier

    def voucher(self, key, side):
        rows, path = ((self.source_rows, self.voucher_source) if side == 'source'
                      else (self.target_rows, self.voucher_target))
        if not path or key not in rows:
            return 'Not available'
        values = [nested(row, path) for row in rows[key]]
        shown = [str(v) for v in values if v is not None]
        return ', '.join(dict.fromkeys(shown)) if shown else 'Not available'

    def preconditions(self):
        return list(PRECONDITIONS)

    def query_step(self, side, key, outcome):
        db, path = (self.source_db, self.sk) if side == 'source' else (self.target_db, self.tk)
        return f"Query {db} for the record with '{path}' = {key!r}: {outcome}."

    def add(self, category, key, voucher, affected_field, expected_value, actual_value, title, description,
            steps, expected_result, actual_result, business_impact=NOT_DETERMINED, cause=None, extra=None):
        severity = SEVERITY.get(category, DEFAULT_SEVERITY)
        prefix = f'[{severity.upper()}] ' if category in SEVERITY else ''
        draft = {
            'defect_id': f'DRAFT-{len(self.defects) + 1:03d}',
            'defect_title': f'{prefix}[{CATEGORY_LABELS[category]}] {title}',
            'severity': severity,
            'record_identifier': self.record_identifier(key, voucher),
            # The record's Source business key value, whatever the payload (invoice, product, customer...).
            'business_key': key,
        }
        if self.voucher_source:
            # Only when the STTM maps a Voucher field; otherwise the draft has no voucher at all.
            draft['voucher'] = voucher
        draft.update({
            'issue_category': category,
            'affected_field': affected_field,
            'expected_value': expected_value,
            'actual_value': actual_value,
        })
        draft.update(extra or {})
        draft.update({
            'defect_description': description,
            'preconditions': self.preconditions(),
            'steps_to_reproduce': steps,
            'expected_result': expected_result,
            'actual_result': actual_result,
            'probable_cause': f'Hypothesis (unverified): {cause or PROBABLE_CAUSES[category]}',
            'business_impact': business_impact,
            'status': STATUS,
        })
        self.defects.append(draft)

    def field_defect(self, key, difference):
        sf, tf = difference.get('source_field'), difference.get('target_field')
        field_type = self.types.get((sf, tf), 'string')
        sv, tv = difference.get('source_value'), difference.get('target_value')
        category = classify_difference(difference, field_type)
        # Tell an explicit null from an absent attribute from the raw record, or else from reconcile()'s flags.
        raw_sv = (_lookup(self.source_rows[key][0], sf) if key in self.source_rows
                  else _MISSING if difference.get('source_absent') else sv)
        raw_tv = (_lookup(self.target_rows[key][0], tf) if key in self.target_rows
                  else _MISSING if difference.get('target_absent') else tv)
        voucher = self.voucher(key, 'source')
        if voucher == 'Not available':
            voucher = self.voucher(key, 'target')
        field_label = f'{sf} (Source) -> {tf} (Target)'
        present = f'{self.record(key, start=True)} is present in {self.source_db} and in {self.target_db}'
        if difference.get('transformation'):
            self.transformed_field_defect(key, difference, field_type, category, raw_sv, raw_tv, voucher,
                                          field_label, present)
            return
        expected_result = f"Target '{tf}' equals Source '{sf}' ({_fmt(raw_sv)}) when compared as {field_type}."
        actual_result = f"Target '{tf}' is {_fmt(raw_tv)}; Source '{sf}' is {_fmt(raw_sv)}."
        impact, cause = NOT_DETERMINED, None
        compare_step = f"Compare Source '{sf}' with Target '{tf}' as {field_type}."
        if category == NULL_VALUE_MISMATCH and sv is None and tv is None:
            title = (f"{key}: Target '{tf}' is absent but Source '{sf}' is an explicit null" if raw_tv is _MISSING
                     else f"{key}: Source '{sf}' is absent but Target '{tf}' is an explicit null")
            description = (f'{present}, but the mapped field {field_label} is an explicit null on one side and '
                           f'absent on the other. Source value: {_fmt(raw_sv)}. Target value: {_fmt(raw_tv)}.')
            expected_result = (f"Target '{tf}' is {_fmt(raw_sv)}, as Source '{sf}' is; an explicit null and an "
                               'absent attribute are not the same.')
            compare_step = (f"Check whether Source '{sf}' and Target '{tf}' are present: an explicit null and an "
                            'absent attribute are not the same.')
            cause = NULL_ABSENT_CAUSE
        elif category == NULL_VALUE_MISMATCH:
            title = (f"{key}: Target '{tf}' is null/absent but Source '{sf}' is populated" if tv is None
                     else f"{key}: Source '{sf}' is null but Target '{tf}' is populated")
            description = (f'{present}, but the mapped field {field_label} is null or absent on one side only. '
                           f'Source value: {_fmt(raw_sv)}. Target value: {_fmt(raw_tv)}.')
        elif category == DATA_TYPE_MISMATCH:
            title = f"{key}: Target '{tf}' type differs from Source '{sf}'"
            description = (f'{present}, but the mapped field {field_label} (mapped type: {field_type}) holds '
                           f'values of incompatible types. Source value: {_fmt(raw_sv)} ({_type_name(sv)}). '
                           f'Target value: {_fmt(raw_tv)} ({_type_name(tv)}).')
            actual_result += f' Observed types: Source {_type_name(sv)}, Target {_type_name(tv)}.'
        else:
            title = f"{key}: Target '{tf}' does not match Source '{sf}'"
            description = (f'{present}, but the mapped field {field_label} differs after {field_type} normalization. '
                           f'Source value: {_fmt(raw_sv)}. Target value: {_fmt(raw_tv)}.')
            if field_type == 'decimal':
                variance, exact = _decimal_variance(sv, tv)
                if variance is None:
                    observed = 'Numeric variance observed; the exact difference could not be computed'
                else:
                    observed = (f"Numeric variance observed: Target value differs from Source by "
                                f"{'' if exact else 'approximately '}{variance}")
                impact = (f'{observed} ({_fmt(raw_tv)} vs {_fmt(raw_sv)}). Any consumer reading this field from '
                          f'{self.target_db} receives a value different from {self.source_db}, the system of record.')
        steps = [self.query_step('source', key, 'the record is present'),
                 self.query_step('target', key, 'the record is present'),
                 compare_step]
        self.add(category, key, voucher, field_label, _plain(raw_sv), _plain(raw_tv), title, description,
                 steps, expected_result, actual_result, impact, cause=cause)

    def transformed_field_defect(self, key, difference, field_type, category, raw_sv, raw_tv, voucher, field_label, present):
        """Draft for a field whose STTM transformation result (not the raw Source value) differs from Target."""
        sf, tf = difference.get('source_field'), difference.get('target_field')
        rule, failure = difference['transformation'], difference.get('transformation_error')
        tv = difference.get('target_value')
        expected = difference.get('expected_value')
        if failure:
            shown = TRANSFORM_FAILED
        else:
            # A rule leaves an absent Source attribute as nothing, which stays "absent" rather than "NULL".
            shown = _MISSING if expected is None and raw_sv is _MISSING else expected
        shown_text = (shown if field_type == 'decimal' and isinstance(shown, str) and _PLAIN_NUMBER.fullmatch(shown)
                      else _fmt(shown))
        the_rule = f'the STTM transformation {rule!r}'
        values = (f'Source value: {_fmt(raw_sv)}. Expected Target value after transformation: {shown_text}. '
                  f'Target value: {_fmt(raw_tv)}.')
        expected_result = (f"Target '{tf}' equals {the_rule} applied to Source '{sf}' ({_fmt(raw_sv)}), "
                           f'i.e. {shown_text}, when compared as {field_type}.')
        actual_result = f"Target '{tf}' is {_fmt(raw_tv)}; Source '{sf}' is {_fmt(raw_sv)}."
        impact, cause = NOT_DETERMINED, None
        if failure:
            title = f"{key}: STTM transformation cannot be applied to Source '{sf}'"
            description = (f'{present}, but {the_rule} for the mapped field {field_label} cannot be applied to the '
                           f'Source value {_fmt(raw_sv)}: {failure}. Target value: {_fmt(raw_tv)}.')
            expected_result = (f"The STTM transformation {rule!r} can be applied to Source '{sf}' ({_fmt(raw_sv)}) and "
                               f"Target '{tf}' equals the result when compared as {field_type}.")
            actual_result += f' The transformation cannot be applied: {failure}.'
            cause = TRANSFORM_FAILED_CAUSE
        elif category == NULL_VALUE_MISMATCH and expected is None and tv is None:
            title = (f"{key}: Target '{tf}' is absent but transformed Source '{sf}' is an explicit null"
                     if raw_tv is _MISSING
                     else f"{key}: transformed Source '{sf}' is absent but Target '{tf}' is an explicit null")
            description = (f'{present}, but after {the_rule} the mapped field {field_label} is an explicit null on '
                           f'one side and absent on the other. {values}')
            expected_result += ' An explicit null and an absent attribute are not the same.'
            actual_result += f' Expected Target value after transformation: {shown_text}.'
            cause = NULL_ABSENT_CAUSE
        elif category == NULL_VALUE_MISMATCH:
            title = (f"{key}: Target '{tf}' is null/absent but transformed Source '{sf}' is populated" if tv is None
                     else f"{key}: transformed Source '{sf}' is null/absent but Target '{tf}' is populated")
            description = (f'{present}, but after {the_rule} the mapped field {field_label} is null or absent on one '
                           f'side only. {values}')
            actual_result += f' Expected Target value after transformation: {shown_text}.'
        elif category == DATA_TYPE_MISMATCH:
            title = f"{key}: Target '{tf}' type differs from transformed Source '{sf}'"
            description = (f'{present}, but the mapped field {field_label} (mapped type: {field_type}) holds a value of '
                           f'an incompatible type after {the_rule}. {values}')
            actual_result += f' Expected Target value after transformation: {shown_text}.'
        else:
            title = f"{key}: Target '{tf}' does not match transformed Source '{sf}'"
            description = (f'{present}, but the mapped field {field_label} does not hold the result of {the_rule} '
                           f'when compared as {field_type}. {values}')
            actual_result += f' Expected Target value after transformation: {shown_text}.'
            cause = TRANSFORM_MISMATCH_CAUSE
            if field_type == 'decimal':
                variance, exact = _decimal_variance(expected, tv)
                if variance is None:
                    observed = 'Numeric variance observed; the exact difference could not be computed'
                else:
                    observed = (f"Numeric variance observed: Target value differs from the transformed Source value by "
                                f"{'' if exact else 'approximately '}{variance}")
                impact = (f'{observed} ({_fmt(raw_tv)} vs {shown_text}). Any consumer reading this field from '
                          f'{self.target_db} receives a value different from the one the STTM derives from '
                          f'{self.source_db}, the system of record.')
        steps = [self.query_step('source', key, 'the record is present'),
                 self.query_step('target', key, 'the record is present'),
                 f"Apply {the_rule} to Source '{sf}' and compare the result with Target '{tf}' as {field_type}."]
        self.add(category, key, voucher, field_label, _plain(shown), _plain(raw_tv), title, description, steps,
                 expected_result, actual_result, impact, cause=cause,
                 extra={'source_value': _plain(raw_sv), 'transformation': rule})

    def missing_defect(self, key):
        self.add(MISSING_IN_TARGET, key, self.voucher(key, 'source'), f'{self.sk} -> {self.tk} (business key)',
                 key, 'Record not found in Target', f'{key} exists in Source but not in Target',
                 f"{self.record(key, start=True)} is present in {self.source_db} but not in {self.target_db}: "
                 f"no Target record has '{self.tk}' = {key!r}.",
                 [self.query_step('source', key, 'the record is present'),
                  self.query_step('target', key, 'no record is returned')],
                 f"A record with '{self.tk}' = {key!r} is present in {self.target_db}.",
                 f"No record with '{self.tk}' = {key!r} is present in {self.target_db}.",
                 f'{self.record(key, start=True)} is present in {self.source_db} but absent from {self.target_db}, so any consumer '
                 f'reading {self.target_db} does not see this record.')

    def extra_defect(self, key):
        self.add(EXTRA_IN_TARGET, key, self.voucher(key, 'target'), f'{self.tk} -> {self.sk} (business key)',
                 'Record not found in Source', key, f'{key} exists in Target but not in Source',
                 f"{self.record(key, start=True)} is present in {self.target_db} but not in {self.source_db}: "
                 f"no Source record has '{self.sk}' = {key!r}.",
                 [self.query_step('target', key, 'the record is present'),
                  self.query_step('source', key, 'no record is returned')],
                 f"Every record in {self.target_db} has a matching record in {self.source_db}; "
                 f"no record with '{self.tk}' = {key!r} is expected without one.",
                 f"The record with '{self.tk}' = {key!r} is present in {self.target_db} "
                 f'without a matching record in {self.source_db}.',
                 f'{self.target_db} exposes {self.record(key)}, which has no corresponding record in '
                 f'{self.source_db}, the system of record.')

    def duplicate_defect(self, key, side, extra_occurrences):
        label, path, db = (('Source', self.sk, self.source_db) if side == 'source'
                           else ('Target', self.tk, self.target_db))
        rows = self.source_rows if side == 'source' else self.target_rows
        occurrences = len(rows[key]) if key in rows else extra_occurrences + 1
        expected = f'Exactly 1 record per business key in {db}.'
        actual = f"{occurrences} records with '{path}' = {key!r} are present in {db}."
        self.add(DUPLICATE_BUSINESS_KEY, key, self.voucher(key, side), f'{path} ({label} business key)',
                 1, occurrences, f"{key} appears {occurrences} times in {label}",
                 f"{self.record(key, start=True)} is present {occurrences} times in {db}: the business key '{path}' is not unique. "
                 'Reconciliation compared only the first occurrence, so field-level results for this key are not reliable.',
                 [self.query_step(side, key, f'{occurrences} records are returned')],
                 expected, actual,
                 f'Field-level validation for {self.record(key)} could not be completed because the business key is not unique in {db}.')


def build_defect_report(result, mapping=None, source=None, target=None, context=None):
    """Return {'validation_status', 'incomplete_reasons', 'anomalies', 'defects', 'suppressed'}.

    validation_status is PASS, FAIL or INCOMPLETE. When the result is INCOMPLETE,
    only defects that remain reliable are generated; the rest are listed in
    'suppressed' with the reason.
    """
    structural, partial = _check_completeness(result, source, target)
    if structural:
        return {'validation_status': 'INCOMPLETE', 'incomplete_reasons': structural + partial, 'anomalies': [], 'defects': [],
                'suppressed': [{'item': 'all defects', 'reason': 'Reconciliation result is structurally incomplete.'}]}
    reasons = partial

    builder = _Builder(mapping, source, target, context)
    key_issues = result.get('key_issues') or {}
    duplicate_keys, suppressed = set(), []
    for side in ('source', 'target'):
        counts = {}
        for issue in key_issues.get(side, []):
            if issue.get('reason') == 'duplicate business key' and issue.get('key') is not None:
                counts[issue['key']] = counts.get(issue['key'], 0) + 1
        for key in sorted(counts):
            duplicate_keys.add(key)
            builder.duplicate_defect(key, side, counts[key])
    source_unkeyed = any(i.get('key') is None for i in key_issues.get('source', []))
    target_unkeyed = any(i.get('key') is None for i in key_issues.get('target', []))

    for mismatch in result.get('mismatches', []):
        key = mismatch['key']
        if key in duplicate_keys:
            suppressed.append({'item': f'field differences for {key}', 'reason': 'Business key is duplicated.'})
            continue
        for difference in mismatch.get('differences', []):
            builder.field_defect(key, difference)
    for key in result.get('missing_in_target', []):
        if target_unkeyed:
            suppressed.append({'item': f'missing-in-target for {key}',
                               'reason': 'Target has records without a business key; the record may exist among them.'})
        else:
            builder.missing_defect(key)
    for key in result.get('extra_in_target', []):
        if source_unkeyed:
            suppressed.append({'item': f'extra-in-target for {key}',
                               'reason': 'Source has records without a business key; the source record may exist among them.'})
        else:
            builder.extra_defect(key)

    anomalies = []
    extra_keys = [d['business_key'] for d in builder.defects if d['issue_category'] == EXTRA_IN_TARGET]
    if extra_keys:
        anomalies.append(f"Unexpected scenario: {len(extra_keys)} target key(s) have no source record "
                         f"({', '.join(extra_keys)}). Target data is expected to be derived from source. "
                         'Verify that the source and target extracts cover the same scope and environment '
                         'before triaging the other defects.')

    if reasons:
        status = 'INCOMPLETE'
    else:
        status = 'FAIL' if builder.defects else 'PASS'
    return {'validation_status': status, 'incomplete_reasons': reasons, 'anomalies': anomalies,
            'defects': builder.defects, 'suppressed': suppressed}


def generate_defect_drafts(result, mapping=None, source=None, target=None, context=None):
    """Return only the list of defect drafts (see build_defect_report)."""
    return build_defect_report(result, mapping, source, target, context)['defects']


def defects_to_csv(defects):
    columns = [c for c in CSV_COLUMNS if c not in OPTIONAL_COLUMNS or any(c in d for d in defects)]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    for defect in defects:
        row = dict(defect)
        for name in ('preconditions', 'steps_to_reproduce'):
            row[name] = '\n'.join(f'{i}. {s}' for i, s in enumerate(row.get(name, []), start=1))
        for name in ('expected_value', 'actual_value'):
            row[name] = 'NULL' if row.get(name) is None else row[name]
        if 'source_value' in row and row['source_value'] is None:
            row['source_value'] = 'NULL'
        writer.writerow({name: _csv_safe(row.get(name)) for name in columns})
    return buffer.getvalue()
