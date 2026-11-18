"""STTM transformation rules: turn a Source value into the value Target is expected to hold.

A rule is written in the STTM 'transformation' column. Steps are chained with '|' and applied
left to right, for example:

    trim | upper
    multiply(100) | round(2)
    map(A=Active; I=Inactive; *=Unknown)
    concat(FirstName, ' ', LastName)
    default('N/A')
    to_date(DD/MM/YYYY)

Only the named rules below are accepted and nothing in the sheet is run as code, so the same
inputs always give the same result. A null or absent Source value stays null through every
rule except default, coalesce and concat.
"""
import re
from collections import namedtuple
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from functools import lru_cache

# Cell text meaning the value is copied as it is.
DIRECT = {'', 'direct', 'direct move', 'direct mapping', 'direct map', 'straight move', 'as is', 'as-is',
          'none', 'n/a', 'na', '-', '1:1', 'no transformation', 'copy'}

# The keyword for the mapped Source field's own value inside concat() and coalesce().
SELF = 'value'

RULE_HELP = ('upper, lower, trim, ltrim, rtrim, prefix(text), suffix(text), replace(old, new), left(n), right(n), '
             'substring(start, length), lpad(width, char), rpad(width, char), map(code=value; ...; *=default), '
             'multiply(n or field), divide(n or field), add(n or field), subtract(n or field), round(digits), '
             'abs, negate, default(value), '
             'coalesce(field or literal, ...), concat(field or literal, ...), to_date(format), format_date(format). '
             "Chain steps with '|', e.g. 'trim | upper'")

_STEP = re.compile(r'^([A-Za-z_]+)\s*(?:\((.*)\))?$', re.S)
_FIELD_NAME = re.compile(r'[A-Za-z_][\w.]*')
_DATE_TOKENS = re.compile(r'YYYY|HH24|MON|YY|MM|DD|HH|MI|SS', re.IGNORECASE)
_DATE_CODES = {'YYYY': '%Y', 'HH24': '%H', 'MON': '%b', 'YY': '%y', 'MM': '%m', 'DD': '%d', 'HH': '%H', 'MI': '%M', 'SS': '%S'}

Step = namedtuple('Step', 'label fn null_aware')
_ABSENT = object()


class TransformError(ValueError):
    """A Source value the rule cannot be applied to, e.g. multiply(100) on 'abc'."""


def is_direct(text):
    return text is None or str(text).strip().lower() in DIRECT


# ---- parsing --------------------------------------------------------------------------------------

def _split(text, separators):
    """Split text on any separator character outside quotes and parentheses."""
    parts, current, quote, depth = [], [], None, 0
    for char in text:
        if quote:
            if char == quote:
                quote = None
        elif char in '\'"':
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char in separators and depth == 0:
            parts.append(''.join(current).strip())
            current = []
            continue
        current.append(char)
    if quote:
        raise ValueError(f'unclosed quote in {text!r}')
    parts.append(''.join(current).strip())
    return parts


def _unquote(token):
    """(text, quoted?) for an argument; quotes keep leading/trailing spaces and commas."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in '\'"':
        return token[1:-1], True
    return token, False


def _args(name, raw, low, high):
    args = [] if raw is None or not raw.strip() else _split(raw, ',')
    if not low <= len(args) <= high:
        wanted = f'{low}' if low == high else f'{low} to {high}'
        raise ValueError(f'{name}() takes {wanted} argument(s), got {len(args)}')
    if any(arg == '' for arg in args):
        raise ValueError(f'{name}() has an empty argument; quote a blank value as \'\'')
    return args


def _number_arg(name, token):
    text = _unquote(token)[0].strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(f'{name}() needs a number, got {token!r}') from None
    if not number.is_finite():
        raise ValueError(f'{name}() needs a finite number, got {token!r}')
    return number


def _int_arg(name, token, low, high):
    number = _number_arg(name, token)
    if number != number.to_integral_value() or not low <= number <= high:
        raise ValueError(f'{name}() needs a whole number from {low} to {high}, got {token!r}')
    return int(number)


def _date_format(name, token):
    text = _unquote(token)[0]
    if not text.strip():
        raise ValueError(f'{name}() needs a date format, e.g. DD/MM/YYYY or %d/%m/%Y')
    if '%' in text:
        return text
    return _DATE_TOKENS.sub(lambda m: _DATE_CODES[m.group(0).upper()], text)


# ---- values -----------------------------------------------------------------------------------------

def _text(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (dict, list)):
        raise TransformError(f'expected a single value, got {type(value).__name__} {value!r}')
    return str(value)


def _to_number(value, label):
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        raise TransformError(f'{label} needs a number, got {value!r}')
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise TransformError(f'{label} needs a number, got {value!r}') from None
    if not number.is_finite():
        raise TransformError(f'{label} needs a finite number, got {value!r}')
    return number


def _lookup(record, path):
    value = record
    for part in path.split('.'):
        if isinstance(value, list):
            if len(value) != 1:
                return _ABSENT if not value else TransformError(f"field '{path}' has several values")
            value = value[0]
        if not isinstance(value, dict) or part not in value:
            return _ABSENT
        value = value[part]
    return value


def _field(record, source_path, name):
    """A field named in concat()/coalesce(): next to the mapped Source field first, then from the top."""
    parent = source_path.rsplit('.', 1)[0] if '.' in source_path else ''
    for path in ([f'{parent}.{name}'] if parent else []) + [name]:
        found = _lookup(record, path)
        if isinstance(found, TransformError):
            raise found
        if found is not _ABSENT:
            return found
    return None


def _operand(token):
    """A concat()/coalesce() argument: a quoted literal, the keyword 'value', or a Source field name."""
    text, quoted = _unquote(token)
    if quoted:
        return lambda value, record, path: text
    if text.lower() == SELF:
        return lambda value, record, path: value
    return lambda value, record, path: _field(record or {}, path or '', text)


# ---- rules ------------------------------------------------------------------------------------------

def _text_rule(fn):
    def build(name, raw):
        _args(name, raw, 0, 0)
        return Step(name, lambda value, record, path: fn(_text(value)), False)
    return build


def _literal_rule(fn, count):
    def build(name, raw):
        args = [_unquote(a)[0] for a in _args(name, raw, count, count)]
        return Step(f'{name}({raw})', lambda value, record, path: fn(_text(value), *args), False)
    return build


def _slice_rule(name, raw):
    if name in ('left', 'right'):
        [n] = [_int_arg(name, a, 0, 100000) for a in _args(name, raw, 1, 1)]
        if name == 'left':
            return Step(f'{name}({raw})', lambda value, record, path: _text(value)[:n], False)
        return Step(f'{name}({raw})', lambda value, record, path: _text(value)[-n:] if n else '', False)
    args = _args(name, raw, 1, 2)
    start = _int_arg(name, args[0], 1, 100000)
    length = _int_arg(name, args[1], 0, 100000) if len(args) == 2 else None
    end = None if length is None else start - 1 + length
    return Step(f'{name}({raw})', lambda value, record, path: _text(value)[start - 1:end], False)


def _pad_rule(name, raw):
    args = _args(name, raw, 1, 2)
    width = _int_arg(name, args[0], 0, 100000)
    char = _unquote(args[1])[0] if len(args) == 2 else ('0' if name == 'lpad' else ' ')
    if len(char) != 1:
        raise ValueError(f'{name}() pads with a single character, got {char!r}')
    if name == 'lpad':
        return Step(f'{name}({raw})', lambda value, record, path: _text(value).rjust(width, char), False)
    return Step(f'{name}({raw})', lambda value, record, path: _text(value).ljust(width, char), False)


def _map_rule(name, raw):
    if raw is None or not raw.strip():
        raise ValueError('map() needs at least one code=value pair, e.g. map(A=Active; I=Inactive)')
    entries = _split(raw, ';' if ';' in raw else ',')
    table, default = {}, _ABSENT
    for entry in entries:
        if not entry:
            continue
        pair = _split(entry, '=')
        if len(pair) != 2:
            raise ValueError(f'map() entry {entry!r} must look like code=value')
        code, result = (_unquote(p)[0] for p in pair)
        if code == '*' and not _unquote(pair[0])[1]:
            default = result
        elif code in table:
            raise ValueError(f'map() lists the code {code!r} twice')
        else:
            table[code] = result
    # A code with no entry and no '*' default is kept unchanged, so it shows up as a mismatch if Target differs.
    return Step(f'{name}({raw})', lambda value, record, path: table.get(
        _text(value), value if default is _ABSENT else default), False)


def _numeric_operand(name, raw):
    """multiply/divide/add/subtract take a number, or the name of a Source field holding one."""
    [token] = _args(name, raw, 1, 1)
    try:
        number = _number_arg(name, token)
    except ValueError:
        text, quoted = _unquote(token)
        if quoted or not _FIELD_NAME.fullmatch(text):
            raise ValueError(f'{name}() needs a number or a Source field name, got {token!r}') from None
        field = _operand(token)

        def get(value, record, path):
            found = field(value, record, path)
            return None if found is None else _to_number(found, f"{name}({raw}): field '{text}'")
        return get
    if name == 'divide' and number == 0:
        raise ValueError('divide() by zero')
    return lambda value, record, path: number


def _arith_rule(name, raw):
    if name in ('abs', 'negate'):
        _args(name, raw, 0, 0)
        get_operand = None
    else:
        get_operand = _numeric_operand(name, raw)
    label = f'{name}({raw})' if get_operand else name

    def fn(value, record, path):
        number = _to_number(value, label)
        operand = get_operand(value, record, path) if get_operand else None
        if get_operand and operand is None:
            return None  # like SQL, arithmetic with a null field gives null
        if name == 'divide' and operand == 0:
            raise TransformError(f'{label} divides by zero')
        with localcontext() as ctx:
            ctx.prec = 60
            if name == 'multiply':
                return number * operand
            if name == 'divide':
                return number / operand
            if name == 'add':
                return number + operand
            if name == 'subtract':
                return number - operand
            return abs(number) if name == 'abs' else -number
    return Step(label, fn, False)


def _round_rule(name, raw):
    [digits] = [_int_arg(name, a, 0, 30) for a in _args(name, raw, 1, 1)]

    def fn(value, record, path):
        number = _to_number(value, f'{name}({raw})')
        with localcontext() as ctx:
            ctx.prec = 80
            try:
                return number.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
            except InvalidOperation:
                raise TransformError(f'{name}({raw}) cannot round {value!r}') from None
    return Step(f'{name}({raw})', fn, False)


def _default_rule(name, raw):
    [token] = _args(name, raw, 1, 1)
    literal = _unquote(token)[0]
    return Step(f'{name}({raw})', lambda value, record, path: literal if value is None else value, True)


def _coalesce_rule(name, raw):
    operands = [_operand(a) for a in _args(name, raw, 1, 50)]

    def fn(value, record, path):
        for operand in operands:
            found = operand(value, record, path)
            if found is not None:
                return found
        return None
    return Step(f'{name}({raw})', fn, True)


def _concat_rule(name, raw):
    operands = [_operand(a) for a in _args(name, raw, 1, 50)]
    # Like SQL CONCAT, a null part counts as empty text.
    return Step(f'{name}({raw})', lambda value, record, path: ''.join(
        '' if part is None else _text(part) for part in (op(value, record, path) for op in operands)), True)


def _to_date_rule(name, raw):
    [token] = _args(name, raw, 1, 1)
    fmt = _date_format(name, token)

    def fn(value, record, path):
        try:
            return datetime.strptime(_text(value).strip(), fmt).date().isoformat()
        except ValueError:
            raise TransformError(f'{name}({raw}) cannot read {value!r} as a date in that format') from None
    return Step(f'{name}({raw})', fn, False)


def _format_date_rule(name, raw):
    [token] = _args(name, raw, 1, 1)
    fmt = _date_format(name, token)

    def fn(value, record, path):
        try:
            day = value if isinstance(value, date) else date.fromisoformat(_text(value).strip()[:10])
        except ValueError:
            raise TransformError(f'{name}({raw}) needs an ISO date (YYYY-MM-DD), got {value!r}') from None
        return day.strftime(fmt)
    return Step(f'{name}({raw})', fn, False)


RULES = {
    'upper': _text_rule(str.upper), 'lower': _text_rule(str.lower), 'trim': _text_rule(str.strip),
    'ltrim': _text_rule(str.lstrip), 'rtrim': _text_rule(str.rstrip),
    'prefix': _literal_rule(lambda text, p: p + text, 1), 'suffix': _literal_rule(lambda text, s: text + s, 1),
    'replace': _literal_rule(lambda text, old, new: text.replace(old, new) if old else text, 2),
    'left': _slice_rule, 'right': _slice_rule, 'substring': _slice_rule,
    'lpad': _pad_rule, 'rpad': _pad_rule,
    'map': _map_rule,
    'multiply': _arith_rule, 'divide': _arith_rule, 'add': _arith_rule, 'subtract': _arith_rule,
    'abs': _arith_rule, 'negate': _arith_rule, 'round': _round_rule,
    'default': _default_rule, 'coalesce': _coalesce_rule, 'concat': _concat_rule,
    'to_date': _to_date_rule, 'format_date': _format_date_rule,
}
ALIASES = {'uppercase': 'upper', 'ucase': 'upper', 'lowercase': 'lower', 'lcase': 'lower', 'strip': 'trim',
           'substr': 'substring', 'mid': 'substring', 'multiply_by': 'multiply', 'divide_by': 'divide',
           'ifnull': 'default', 'isnull': 'default', 'nvl': 'default', 'lookup': 'map', 'decode': 'map'}


@lru_cache(maxsize=512)
def compile_rule(text):
    """Return the tuple of steps for a transformation cell; () when the value is copied as it is.

    Raises ValueError naming the step that is not understood.
    """
    if is_direct(text):
        return ()
    steps = []
    for part in _split(str(text).strip(), '|'):
        match = _STEP.match(part)
        name = match.group(1).lower() if match else ''
        name = ALIASES.get(name, name)
        if not match or name not in RULES:
            raise ValueError(f'{part!r} is not a supported transformation. Supported: {RULE_HELP}')
        steps.append(RULES[name](name, match.group(2)))
    return tuple(steps)


def apply_rule(text, value, record=None, source_path=''):
    """Apply a transformation to one Source value; record is the Source record (for concat/coalesce).

    Raises TransformError when the value cannot be transformed.
    """
    for step in compile_rule(text):
        if value is None and not step.null_aware:
            continue
        value = step.fn(value, record, source_path)
    return value


def plain(value):
    """JSON-friendly copy of a transformed value: numbers as text, so 15000 stays exactly 15000."""
    return str(value) if isinstance(value, Decimal) else value
