import unittest
from src.validator import normalize, reconcile


class ValidatorTests(unittest.TestCase):
    def test_matching_decimals(self):
        mapping = {'key': {'source': 'id', 'target': 'id'}, 'fields': [{'source': 'amount', 'target': 'amount', 'type': 'decimal'}]}
        result = reconcile([{'id': '1', 'amount': '10.00'}], [{'id': '1', 'amount': 10}], mapping)
        self.assertEqual(result['summary']['passed'], 1)

    def test_non_finite_decimals_are_unparseable(self):
        mapping = {'key': {'source': 'id', 'target': 'id'}, 'fields': [{'source': 'amount', 'target': 'amount', 'type': 'decimal'}]}
        self.assertEqual(normalize('NaN', 'decimal'), ('INVALID_DECIMAL', 'NaN'))
        self.assertEqual(normalize('-Infinity', 'decimal'), ('INVALID_DECIMAL', '-Infinity'))
        result = reconcile([{'id': '1', 'amount': 'NaN'}], [{'id': '1', 'amount': 'NaN'}], mapping)
        self.assertEqual(result['summary']['passed'], 1)  # identical values are not a mismatch

    def test_boolean_normalization(self):
        for value in (True, 1, 1.0, '1', 'true', 'TRUE', ' Yes ', 'y', 't'):
            self.assertIs(normalize(value, 'boolean'), True, value)
        for value in (False, 0, '0', 'false', 'No', 'N', 'f'):
            self.assertIs(normalize(value, 'boolean'), False, value)
        for value in (2, -1, float('nan'), 'maybe', '', '1.0', {'a': 1}, [True, False]):
            self.assertEqual(normalize(value, 'boolean')[0], 'INVALID_BOOLEAN', value)
        self.assertIsNone(normalize(None, 'boolean'))

    def test_boolean_fields_compare_across_representations(self):
        mapping = {'key': {'source': 'id', 'target': 'id'}, 'fields': [{'source': 'active', 'target': 'enabled', 'type': 'boolean'}]}
        source = [{'id': '1', 'active': 1}, {'id': '2', 'active': 'N'}, {'id': '3', 'active': True}]
        target = [{'id': '1', 'enabled': True}, {'id': '2', 'enabled': False}, {'id': '3', 'enabled': False}]
        result = reconcile(source, target, mapping)
        self.assertEqual(result['passed_keys'], ['1', '2'])
        self.assertEqual([m['key'] for m in result['mismatches']], ['3'])

    def test_duplicate_key(self):
        mapping = {'key': {'source': 'id', 'target': 'id'}, 'fields': []}
        result = reconcile([{'id': '1'}, {'id': '1'}], [{'id': '1'}], mapping)
        self.assertEqual(result['summary']['key_issues'], 1)


    MAPPING = {'key': {'source': 'InvoiceNumber', 'target': 'ReceivableHeader.InvoiceNumber'},
               'fields': [{'source': 'Memo', 'target': 'ReceivableHeader.Memo', 'type': 'string'}]}

    def test_header_as_object_or_single_item_array(self):
        source = [{'InvoiceNumber': 'INV1', 'Memo': 'a'}, {'InvoiceNumber': 'INV2', 'Memo': 'b'}]
        target = [{'ReceivableHeader': {'InvoiceNumber': 'INV1', 'Memo': 'a'}},
                  {'ReceivableHeader': [{'InvoiceNumber': 'INV2', 'Memo': 'b'}]}]
        result = reconcile(source, target, self.MAPPING)
        self.assertEqual(result['passed_keys'], ['INV1', 'INV2'])

    def test_multi_item_array_field_is_reported_not_hidden(self):
        mapping = {'key': {'source': 'InvoiceNumber', 'target': 'InvoiceNumber'},
                   'fields': [{'source': 'Memo', 'target': 'ReceivableHeader.Memo', 'type': 'string'}]}
        target = [{'InvoiceNumber': 'INV1', 'ReceivableHeader': [{'Memo': 'a'}, {'Memo': 'b'}]}]
        result = reconcile([{'InvoiceNumber': 'INV1', 'Memo': 'a'}], target, mapping)
        [difference] = result['mismatches'][0]['differences']
        self.assertEqual(difference['target_value'], ['a', 'b'])

    def test_transformation_is_applied_to_source_before_comparing(self):
        mapping = {'key': {'source': 'id', 'target': 'id'}, 'fields': [
            {'source': 'qty', 'target': 'cents', 'type': 'decimal', 'transformation': 'multiply(100)'},
            {'source': 'code', 'target': 'status', 'type': 'string', 'transformation': 'trim | map(A=Active; *=Other)'}]}
        source = [{'id': '1', 'qty': '1.25', 'code': ' A '}, {'id': '2', 'qty': '2', 'code': 'B'}, {'id': '3', 'qty': 'x', 'code': 'A'}]
        target = [{'id': '1', 'cents': 125, 'status': 'Active'}, {'id': '2', 'cents': 2, 'status': 'Other'},
                  {'id': '3', 'cents': 0, 'status': 'Active'}]
        result = reconcile(source, target, mapping)
        self.assertEqual(result['passed_keys'], ['1'])
        by_key = {m['key']: m['differences'] for m in result['mismatches']}
        [qty] = by_key['2']
        self.assertEqual((qty['source_value'], qty['expected_value'], qty['target_value']), ('2', '200', 2))
        self.assertEqual(qty['transformation'], 'multiply(100)')
        [failed] = by_key['3']
        self.assertIn('needs a number', failed['transformation_error'])
        self.assertNotIn('expected_value', failed)

    def test_explicit_null_and_absent_attribute_are_different(self):
        mapping = {'key': {'source': 'id', 'target': 'id'}, 'fields': [{'source': 'memo', 'target': 'h.memo', 'type': 'string'}]}
        source = [{'id': '1', 'memo': None}, {'id': '2'}, {'id': '3', 'memo': None}, {'id': '4'}, {'id': '5', 'memo': None}]
        target = [{'id': '1', 'h': {}}, {'id': '2', 'h': {'memo': None}}, {'id': '3', 'h': {'memo': None}}, {'id': '4', 'h': {}},
                  {'id': '5', 'h': []}]  # an empty array holds no attribute
        result = reconcile(source, target, mapping)
        self.assertEqual(result['passed_keys'], ['3', '4'])
        by_key = {m['key']: m['differences'][0] for m in result['mismatches']}
        self.assertEqual(sorted(by_key), ['1', '2', '5'])
        self.assertEqual((by_key['1'].get('source_absent'), by_key['1'].get('target_absent')), (None, True))
        self.assertEqual((by_key['2'].get('source_absent'), by_key['2'].get('target_absent')), (True, None))
        self.assertTrue(by_key['5']['target_absent'])

    def test_transformed_null_and_absent_attribute_are_different(self):
        mapping = {'key': {'source': 'id', 'target': 'id'}, 'fields': [
            {'source': 'code', 'target': 'code', 'type': 'string', 'transformation': 'upper'},
            {'source': 'region', 'target': 'region', 'type': 'string', 'transformation': "default('N/A')"}]}
        source = [{'id': '1', 'code': None, 'region': None}, {'id': '2', 'region': None},
                  {'id': '3', 'code': None, 'region': None}, {'id': '4', 'region': None}]
        target = [{'id': '1', 'region': 'N/A'}, {'id': '2', 'code': None, 'region': 'N/A'},
                  {'id': '3', 'code': None, 'region': 'N/A'}, {'id': '4', 'region': 'N/A'}]
        result = reconcile(source, target, mapping)
        self.assertEqual(result['passed_keys'], ['3', '4'])  # null stays null, absent stays absent through upper
        by_key = {m['key']: m['differences'] for m in result['mismatches']}
        self.assertEqual([d['target_field'] for d in by_key['1'] + by_key['2']], ['code', 'code'])
        self.assertIsNone(by_key['1'][0]['expected_value'])

    def test_comparisons_list_every_mapped_field_of_every_record(self):
        mapping = {'key': {'source': 'id', 'target': 'id'}, 'fields': [
            {'source': 'name', 'target': 'name', 'type': 'string'},
            {'source': 'qty', 'target': 'cents', 'type': 'decimal', 'transformation': 'multiply(100)'}]}
        source = [{'id': '1', 'name': 'a', 'qty': '1.5'}, {'id': '2', 'name': 'b', 'qty': 'x'}, {'id': '3', 'name': 'c'}]
        target = [{'id': '1', 'name': 'a', 'cents': 150}, {'id': '2', 'name': 'B', 'cents': 1}, {'id': '4', 'cents': 9}]
        comparisons = reconcile(source, target, mapping)['comparisons']
        self.assertEqual([(c['key'], c['source_field'], c['result']) for c in comparisons], [
            ('1', 'name', 'MATCH'), ('1', 'qty', 'MATCH'), ('2', 'name', 'MISMATCH'), ('2', 'qty', 'MISMATCH'),
            ('3', 'name', 'MISSING IN TARGET'), ('3', 'qty', 'MISSING IN TARGET'),
            ('4', 'name', 'EXTRA IN TARGET'), ('4', 'qty', 'EXTRA IN TARGET')])
        matched = comparisons[1]
        self.assertEqual((matched['source_value'], matched['transformation'], matched['expected_value'], matched['target_value']),
                         ('1.5', 'multiply(100)', '150.0', 150))  # the rule result is kept for a match too
        self.assertIn('needs a number', comparisons[3]['transformation_error'])
        self.assertEqual(comparisons[4]['source_value'], 'c')
        self.assertNotIn('target_value', comparisons[4])
        self.assertTrue(comparisons[5]['source_absent'])
        self.assertTrue(comparisons[6]['target_absent'])
        self.assertEqual(comparisons[7]['target_value'], 9)
        self.assertNotIn('source_value', comparisons[7])

    def test_multi_item_array_key_is_a_key_issue(self):
        target = [{'ReceivableHeader': [{'InvoiceNumber': 'INV1'}, {'InvoiceNumber': 'INV2'}]}]
        result = reconcile([{'InvoiceNumber': 'INV1', 'Memo': 'a'}], target, self.MAPPING)
        self.assertEqual(result['summary']['key_issues'], 1)
        self.assertEqual(result['missing_in_target'], ['INV1'])


if __name__ == '__main__':
    unittest.main()
