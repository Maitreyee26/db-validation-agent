import unittest
from decimal import Decimal

from src.transforms import TransformError, apply_rule, compile_rule, is_direct


class TransformTests(unittest.TestCase):
    def test_direct_phrases_mean_no_transformation(self):
        for text in (None, '', '  ', 'Direct', 'direct move', 'As-Is', 'N/A', '-', '1:1', 'No transformation'):
            self.assertTrue(is_direct(text), text)
            self.assertEqual(compile_rule(text), ())
            self.assertEqual(apply_rule(text, ' x '), ' x ')

    def test_text_rules_chain_left_to_right(self):
        self.assertEqual(apply_rule('trim | upper', '  ball valve '), 'BALL VALVE')
        self.assertEqual(apply_rule('UPPERCASE', 'a'), 'A')  # names and aliases are case-insensitive
        self.assertEqual(apply_rule("prefix('PRD-') | suffix(X)", '1'), 'PRD-1X')
        self.assertEqual(apply_rule("replace('-', '')", '12-34-5'), '12345')
        self.assertEqual(apply_rule('left(3)', 'ABCDEF'), 'ABC')
        self.assertEqual(apply_rule('right(2)', 'ABCDEF'), 'EF')
        self.assertEqual(apply_rule('substring(2, 3)', 'ABCDEF'), 'BCD')
        self.assertEqual(apply_rule('lpad(6)', 42), '000042')
        self.assertEqual(apply_rule("rpad(4, '*')", 'ab'), 'ab**')
        self.assertEqual(apply_rule("concat(value, ' | ', value)", 'a'), 'a | a')  # '|' inside quotes is text

    def test_map_with_default_and_passthrough(self):
        rule = 'map(A=Active; I=Inactive; *=Unknown)'
        self.assertEqual(apply_rule(rule, 'A'), 'Active')
        self.assertEqual(apply_rule(rule, 'Z'), 'Unknown')
        self.assertEqual(apply_rule('map(A=Active, I=Inactive)', 'Z'), 'Z')  # no '*': unchanged
        self.assertEqual(apply_rule('map(1=Yes; 0=No)', 1), 'Yes')
        self.assertEqual(apply_rule("map(true='Y'; false='N')", False), 'N')

    def test_numeric_rules_are_exact(self):
        self.assertEqual(apply_rule('multiply(100)', '12.34'), Decimal('1234.00'))
        self.assertEqual(apply_rule('divide(3) | round(2)', 10), Decimal('3.33'))
        self.assertEqual(apply_rule('round(0)', '2.5'), Decimal('3'))  # half away from zero, as SQL ROUND
        self.assertEqual(apply_rule('add(0.1) | subtract(0.3)', '0.2'), Decimal('0.0'))
        self.assertEqual(apply_rule('negate | abs', '-5'), Decimal('5'))

    def test_arithmetic_with_another_source_field(self):
        record = {'Adjustment': {'Quantity': 150, 'UnitCost': '250.5', 'Rate': None, 'Zero': 0}}
        path = 'Adjustment.Quantity'
        self.assertEqual(apply_rule('multiply(UnitCost)', 150, record, path), Decimal('37575.0'))
        self.assertIsNone(apply_rule('multiply(Rate)', 150, record, path))  # null field -> null, as in SQL
        self.assertIsNone(apply_rule('multiply(Missing)', 150, record, path))
        with self.assertRaisesRegex(TransformError, 'divides by zero'):
            apply_rule('divide(Zero)', 150, record, path)
        with self.assertRaisesRegex(TransformError, "field 'Bad' needs a number"):
            apply_rule('add(Bad)', 1, {'Bad': 'x'}, 'Q')

    def test_numeric_rule_on_text_fails_with_reason(self):
        with self.assertRaisesRegex(TransformError, r"multiply\(100\) needs a number, got 'abc'"):
            apply_rule('multiply(100)', 'abc')
        with self.assertRaises(TransformError):
            apply_rule('round(2)', True)

    def test_null_stays_null_except_null_rules(self):
        self.assertIsNone(apply_rule('trim | upper | multiply(2)', None))
        self.assertEqual(apply_rule("default('N/A')", None), 'N/A')
        self.assertEqual(apply_rule("default('N/A')", ''), '')  # only null is replaced, as SQL ISNULL
        self.assertEqual(apply_rule('nvl(0) | add(1)', None), Decimal('1'))

    def test_concat_and_coalesce_read_sibling_fields(self):
        record = {'Customer': {'First': 'Ann', 'Last': 'Lee', 'Nick': None}, 'Region': 'EU'}
        self.assertEqual(apply_rule("concat(First, ' ', Last)", 'Ann', record, 'Customer.First'), 'Ann Lee')
        self.assertEqual(apply_rule("concat(value, '/', Region)", 'Ann', record, 'Customer.First'), 'Ann/EU')
        self.assertEqual(apply_rule("concat(First, Missing, '!')", 'Ann', record, 'Customer.First'), 'Ann!')
        self.assertEqual(apply_rule("coalesce(Nick, First, 'x')", None, record, 'Customer.Nick'), 'Ann')
        self.assertIsNone(apply_rule('coalesce(Nick, Missing)', None, record, 'Customer.Nick'))

    def test_dates(self):
        self.assertEqual(apply_rule('to_date(DD/MM/YYYY)', '05/11/2026'), '2026-11-05')
        self.assertEqual(apply_rule('to_date(%m-%d-%y)', '11-05-26'), '2026-11-05')
        self.assertEqual(apply_rule('to_date(DD-MON-YYYY)', '05-Nov-2026'), '2026-11-05')
        self.assertEqual(apply_rule('format_date(DD/MM/YYYY)', '2026-11-05T10:00:00'), '05/11/2026')
        with self.assertRaisesRegex(TransformError, 'cannot read'):
            apply_rule('to_date(DD/MM/YYYY)', '2026-11-05')
        with self.assertRaisesRegex(TransformError, 'ISO date'):
            apply_rule('format_date(DD/MM/YYYY)', 'yesterday')

    def test_unknown_or_malformed_rules_are_rejected(self):
        for text, message in (('Convert to uppercase', 'not a supported transformation'),
                              ('upper | eval(1)', 'not a supported transformation'),
                              ("multiply('abc')", 'needs a number or a Source field name'),
                              ('multiply(1 2)', 'needs a number or a Source field name'),
                              ('divide(0)', 'by zero'),
                              ('round(2.5)', 'whole number'),
                              ('left()', 'takes 1 argument'),
                              ('upper(1)', 'takes 0 argument'),
                              ('map(A)', 'code=value'),
                              ('map(A=1; A=2)', 'twice'),
                              ("prefix('x)", 'unclosed quote'),
                              ("lpad(4, 'ab')", 'single character')):
            with self.assertRaisesRegex(ValueError, message, msg=text):
                compile_rule(text)

    def test_rule_text_is_never_run_as_code(self):
        with self.assertRaises(ValueError):
            compile_rule("__import__('os').system('echo hi')")


if __name__ == '__main__':
    unittest.main()
