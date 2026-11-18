"""Loads a Source-to-Target Mapping (STTM) from JSON, CSV or Excel into the mapping dict used by reconcile().

Convert an STTM sheet into config/mapping.json (the current file is backed up first):
    python -m src.sttm STTM.xlsx --out config/mapping.json
"""
import argparse
import csv
import io
import json
from datetime import datetime
from pathlib import Path

from .transforms import compile_rule, is_direct

SUPPORTED_TYPES = ('string', 'decimal', 'date', 'boolean')

# Common SQL / Cosmos type names accepted in an STTM sheet, mapped to the validator's comparison types.
TYPE_ALIASES = {
    'string': 'string', 'str': 'string', 'text': 'string', 'varchar': 'string', 'nvarchar': 'string',
    'char': 'string', 'nchar': 'string',
    'decimal': 'decimal', 'numeric': 'decimal', 'number': 'decimal', 'money': 'decimal', 'float': 'decimal',
    'int': 'decimal', 'integer': 'decimal', 'bigint': 'decimal',
    'date': 'date', 'datetime': 'date', 'datetime2': 'date', 'timestamp': 'date',
    'boolean': 'boolean', 'bool': 'boolean', 'bit': 'boolean',
}

COLUMN_ALIASES = {
    'source': ('source', 'source_field', 'source field', 'source column', 'sql field', 'sql column'),
    'target': ('target', 'target_field', 'target field', 'target attribute', 'cosmos field', 'cosmos attribute'),
    'type': ('type', 'data_type', 'data type', 'datatype', 'comparison type'),
    'key': ('key', 'is_key', 'is key', 'business_key', 'business key', 'primary key'),
    # Optional: how Target is derived from Source (see src/transforms.py). Blank or "Direct" = copied as is.
    'transformation': ('transformation', 'transformations', 'transformation rule', 'transformation_rule',
                       'transformation logic', 'transformation_logic', 'transform', 'business rule',
                       'mapping rule', 'mapping logic', 'derivation', 'rule', 'logic'),
}

TRUE_VALUES = {'y', 'yes', 'true', '1', 'x'}

# Type values allowed on a group row: a row naming a parent object or array whose elements are mapped
# in the rows below it, either on one side only (Target "ReceivableHeader") or on both sides with the
# type left blank (Source "ProductDetails" -> Target "item").
GROUP_TYPES = {'', 'object', 'array', 'group', 'record', 'entity'}

CSV_TEMPLATE = (
    'source,target,type,key,transformation\n'
    'InvoiceNumber,invoice.number,string,Y,\n'
    'Voucher,voucher,string,,\n'
    'Amount,payment.amount,decimal,,\n'
    'ApprovalStatus,status,string,,\n'
    'DueDate,payment.dueDate,date,,\n'
)


def _normalize_type(value, row_label):
    kind = str(value or 'string').strip().lower().split('(')[0].strip() or 'string'
    if kind not in TYPE_ALIASES:
        raise ValueError(f"{row_label}: unsupported type '{value}'. Use one of: {', '.join(SUPPORTED_TYPES)}")
    return TYPE_ALIASES[kind]


def _check_transformation(text, row_label):
    """Return the transformation text to store, or None when the value is copied as it is."""
    if is_direct(text):
        return None
    text = str(text).strip()
    try:
        compile_rule(text)
    except ValueError as exc:
        raise ValueError(f'{row_label}: invalid transformation {text!r}: {exc}') from None
    return text


def validate_mapping(mapping):
    if not isinstance(mapping, dict) or not isinstance(mapping.get('key'), dict) or not isinstance(mapping.get('fields'), list):
        raise ValueError("Mapping must contain 'key' and 'fields' entries")
    key = mapping['key']
    if not key.get('source') or not key.get('target'):
        raise ValueError("Mapping 'key' must define both 'source' and 'target'")
    if not is_direct(key.get('transformation')):
        raise ValueError(_KEY_RULE_ERROR.format(where="Mapping 'key'"))
    if not mapping['fields']:
        raise ValueError('Mapping must define at least one field')
    for index, field in enumerate(mapping['fields'], start=1):
        if not isinstance(field, dict) or not field.get('source') or not field.get('target'):
            raise ValueError(f"Mapping field {index} must define 'source' and 'target'")
        field['type'] = _normalize_type(field.get('type'), f'Mapping field {index}')
        rule = _check_transformation(field.get('transformation'), f'Mapping field {index}')
        if rule:
            field['transformation'] = rule
        else:
            field.pop('transformation', None)
        if (field['source'], field['target']) == (key['source'], key['target']) and rule:
            raise ValueError(_KEY_RULE_ERROR.format(where=f'Mapping field {index}'))
    return mapping


_KEY_RULE_ERROR = ('{where}: a transformation on the business key is not supported; '
                   'records are matched on the key values exactly as they are in Source and Target')


def _in_group(group, field):
    """Prefix a field with its group path unless it already carries it."""
    if not group or field == group or field.startswith(group + '.'):
        return field
    return f'{group}.{field}'


def _cells(row, columns):
    source = str(row.get(columns['source']) or '').strip()
    target = str(row.get(columns['target']) or '').strip()
    raw_type = row.get(columns['type']) if 'type' in columns else None
    is_key = str(row.get(columns['key']) or '').strip().lower() in TRUE_VALUES
    rule = row.get(columns['transformation']) if 'transformation' in columns else None
    rule = None if is_direct(rule) else str(rule).strip()
    return source, target, raw_type, str(raw_type or '').strip().lower(), is_key, rule


def _typed_fields_follow(cells, index):
    """True when the next mapping row after cells[index] is a field row naming both sides with a type.

    A blank-type row naming both sides is a parent only when its elements follow it; otherwise (e.g. a
    top-level "SourceSystem -> SourceSystem" row followed by a group row) it is a field compared as string.
    """
    for source, target, _, kind, _, rule in cells[index + 1:]:
        if source or target:
            return bool(source and target and ((kind and kind not in GROUP_TYPES) or rule))
    return False


def _from_rows(rows):
    if not rows:
        raise ValueError('STTM sheet has no mapping rows')
    columns = {}
    for name in rows[0].keys():
        label = str(name or '').strip().lower()
        for canonical, aliases in COLUMN_ALIASES.items():
            if label in aliases:
                columns[canonical] = name
    missing = [c for c in ('source', 'target', 'key') if c not in columns]
    if missing:
        raise ValueError(f"STTM sheet is missing column(s): {', '.join(missing)}. "
                         'Expected: source, target, type, key (and optionally transformation)')
    fields, keys = [], []
    source_group = target_group = ''
    cells = [_cells(row, columns) for row in rows]
    for index, (source, target, raw_type, kind, is_key, rule) in enumerate(cells):
        line = index + 2
        if not source and not target:
            continue
        # A row with a transformation maps a value, so it is a field even with a blank type.
        if kind in GROUP_TYPES and not is_key and not rule:
            if not source or not target:
                # A row naming only one side (e.g. Target "ReceivableHeader") groups the field rows below it.
                if target:
                    target_group = target
                else:
                    source_group = source
                continue
            if kind or _typed_fields_follow(cells, index):
                # A row naming both sides with a blank (or object/array) type, e.g. Source "ProductDetails" ->
                # Target "item", is the parent of the field rows below it until the next parent row.
                source_group, target_group = source, target
                continue
        if not source or not target:
            raise ValueError(f'STTM row {line}: both source and target are required')
        source, target = _in_group(source_group, source), _in_group(target_group, target)
        field = {'source': source, 'target': target, 'type': _normalize_type(raw_type, f'STTM row {line}')}
        if rule:
            if is_key:
                raise ValueError(_KEY_RULE_ERROR.format(where=f'STTM row {line}'))
            field['transformation'] = _check_transformation(rule, f'STTM row {line}')
        fields.append(field)
        if is_key:
            keys.append({'source': source, 'target': target})
    if len(keys) != 1:
        raise ValueError(f"STTM must mark exactly one business key row in the 'key' column (found {len(keys)})")
    return validate_mapping({'key': keys[0], 'fields': fields})


def parse_sttm(data, filename):
    """Parse uploaded STTM bytes. Supports .json (validator mapping format), .csv and .xlsx."""
    name = filename.lower()
    if name.endswith('.json'):
        return validate_mapping(json.loads(data.decode('utf-8-sig')))
    if name.endswith('.csv'):
        return _from_rows(list(csv.DictReader(io.StringIO(data.decode('utf-8-sig')))))
    if name.endswith('.xlsx'):
        from openpyxl import load_workbook
        try:
            sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
            values = list(sheet.iter_rows(values_only=True))
        except Exception as exc:  # openpyxl raises several unrelated types for unreadable workbooks
            raise ValueError(f'Unable to read STTM workbook: {exc}') from exc
        if not values:
            raise ValueError('STTM workbook is empty')
        header = [str(h or '').strip() for h in values[0]]
        return _from_rows([dict(zip(header, row)) for row in values[1:]])
    raise ValueError('Unsupported STTM file type. Upload .json, .csv or .xlsx')


def save_mapping(mapping, path):
    """Write a validated mapping to path, backing up any existing file. Returns the backup path or None."""
    path = Path(path)
    content = json.dumps(validate_mapping(mapping), indent=2) + '\n'
    backup = None
    if path.exists() and path.read_text(encoding='utf-8-sig').strip() != content.strip():
        backup = path.with_name(f"{path.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
        backup.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return backup


def main():
    parser = argparse.ArgumentParser(description='Convert an STTM file (.xlsx, .csv or .json) into the validator mapping JSON.')
    parser.add_argument('sttm_file')
    parser.add_argument('--out', help='Write the mapping here (e.g. config/mapping.json). Without it, the mapping is only printed.')
    args = parser.parse_args()
    try:
        mapping = parse_sttm(Path(args.sttm_file).read_bytes(), args.sttm_file)
    except (OSError, ValueError, UnicodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(mapping, indent=2))
    if args.out:
        backup = save_mapping(mapping, args.out)
        print(f'Mapping written to {args.out}' + (f' (previous version backed up to {backup})' if backup else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
