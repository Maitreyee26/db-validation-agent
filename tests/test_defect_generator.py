import csv
import io
import json
import unittest
from pathlib import Path

from src.defect_generator import (
    DATA_TYPE_MISMATCH, DUPLICATE_BUSINESS_KEY, EXTRA_IN_TARGET, FIELD_VALUE_MISMATCH, MISSING_IN_TARGET,
    NULL_VALUE_MISMATCH, STATUS, build_defect_report, defects_to_csv, generate_defect_drafts,
)
from src.validator import reconcile

ROOT = Path(__file__).resolve().parent.parent

MAPPING = {
    'key': {'source': 'InvoiceNumber', 'target': 'invoice.number'},
    'fields': [
        {'source': 'Voucher', 'target': 'voucher', 'type': 'string'},
        {'source': 'Amount', 'target': 'payment.amount', 'type': 'decimal'},
        {'source': 'ApprovalStatus', 'target': 'status', 'type': 'string'},
        {'source': 'DueDate', 'target': 'payment.dueDate', 'type': 'date'},
    ],
}

REQUIRED_FIELDS = [
    'defect_title', 'record_identifier', 'issue_category', 'affected_field', 'expected_value', 'actual_value',
    'defect_description', 'preconditions', 'steps_to_reproduce', 'expected_result', 'actual_result',
    'probable_cause', 'business_impact', 'status',
]


def sql(number, voucher='PRV001', amount='100.00', status='Approved', due='2026-11-10'):
    return {'InvoiceNumber': number, 'Voucher': voucher, 'Amount': amount, 'ApprovalStatus': status, 'DueDate': due}


def cosmos(number, voucher='PRV001', amount=100, status='Approved', due='2026-11-10'):
    return {'invoice': {'number': number}, 'voucher': voucher, 'payment': {'amount': amount, 'dueDate': due}, 'status': status}


def run(source, target):
    return build_defect_report(reconcile(source, target, MAPPING), MAPPING, source, target)


class DefectGeneratorTests(unittest.TestCase):
    def assertComplete(self, defect):
        for field in REQUIRED_FIELDS:
            self.assertIn(field, defect)
        self.assertEqual(defect['status'], STATUS)
        self.assertTrue(defect['probable_cause'].startswith('Hypothesis'))

    def test_pass_produces_no_defects(self):
        report = run([sql('INV001')], [cosmos('INV001')])
        self.assertEqual(report['validation_status'], 'PASS')
        self.assertEqual(report['defects'], [])

    def test_field_value_mismatch(self):
        report = run([sql('INV001', amount='1500.00')], [cosmos('INV001', amount=1400)])
        self.assertEqual(report['validation_status'], 'FAIL')
        [defect] = report['defects']
        self.assertComplete(defect)
        self.assertEqual(defect['issue_category'], FIELD_VALUE_MISMATCH)
        self.assertEqual(defect['expected_value'], '1500.00')
        self.assertEqual(defect['actual_value'], 1400)
        self.assertEqual(defect['business_key'], 'INV001')
        self.assertEqual(defect['voucher'], 'PRV001')
        self.assertIn('Amount', defect['affected_field'])
        self.assertIn('-100.00', defect['business_impact'])

    def test_non_numeric_value_mismatch_has_no_invented_impact(self):
        report = run([sql('INV001', status='Approved')], [cosmos('INV001', status='Pending')])
        [defect] = report['defects']
        self.assertEqual(defect['issue_category'], FIELD_VALUE_MISMATCH)
        self.assertTrue(defect['business_impact'].startswith('Not determined'))

    def test_missing_record_in_cosmos(self):
        report = run([sql('INV001'), sql('INV003', voucher='PRV003')], [cosmos('INV001')])
        [defect] = report['defects']
        self.assertComplete(defect)
        self.assertEqual(defect['issue_category'], MISSING_IN_TARGET)
        self.assertEqual(defect['business_key'], 'INV003')
        self.assertEqual(defect['voucher'], 'PRV003')

    def test_extra_record_in_cosmos(self):
        report = run([sql('INV001')], [cosmos('INV001'), cosmos('INV004', voucher='PRV004')])
        [defect] = report['defects']
        self.assertComplete(defect)
        self.assertEqual(defect['issue_category'], EXTRA_IN_TARGET)
        self.assertEqual(defect['business_key'], 'INV004')
        self.assertEqual(defect['voucher'], 'PRV004')

    def test_extra_record_is_critical_anomaly(self):
        report = run([sql('INV001')], [cosmos('INV001'), cosmos('INV004'), cosmos('INV005')])
        self.assertEqual(report['validation_status'], 'FAIL')
        self.assertTrue(all(d['severity'] == 'Critical' for d in report['defects']))
        self.assertTrue(report['defects'][0]['defect_title'].startswith('[CRITICAL]'))
        [anomaly] = report['anomalies']
        self.assertIn('2 target key(s)', anomaly)
        self.assertIn('INV004, INV005', anomaly)

    def test_other_categories_are_not_critical_and_raise_no_anomaly(self):
        report = run([sql('INV001', amount='1.00'), sql('INV002')], [cosmos('INV001', amount=2)])
        self.assertEqual(report['anomalies'], [])
        self.assertTrue(all(d['severity'] == 'To be assessed during review' for d in report['defects']))

    def test_suppressed_extra_record_raises_no_anomaly(self):
        report = run([sql('INV001'), {'Voucher': 'PRV004'}], [cosmos('INV001'), cosmos('INV004')])
        self.assertEqual(report['anomalies'], [])

    def test_null_value_mismatch(self):
        report = run([sql('INV001', status='Approved')], [cosmos('INV001', status=None)])
        [defect] = report['defects']
        self.assertComplete(defect)
        self.assertEqual(defect['issue_category'], NULL_VALUE_MISMATCH)
        self.assertEqual(defect['expected_value'], 'Approved')
        self.assertIsNone(defect['actual_value'])

    def test_null_value_mismatch_for_absent_attribute(self):
        target = cosmos('INV001')
        del target['status']
        report = run([sql('INV001')], [target])
        [defect] = report['defects']
        self.assertEqual(defect['issue_category'], NULL_VALUE_MISMATCH)
        self.assertIn('attribute absent', defect['actual_result'])

    def test_data_type_mismatch_invalid_decimal(self):
        report = run([sql('INV001', amount='100.00')], [cosmos('INV001', amount='one hundred')])
        [defect] = report['defects']
        self.assertComplete(defect)
        self.assertEqual(defect['issue_category'], DATA_TYPE_MISMATCH)
        self.assertEqual(defect['actual_value'], 'one hundred')

    def test_data_type_mismatch_invalid_date(self):
        report = run([sql('INV001')], [cosmos('INV001', due='not-a-date')])
        [defect] = report['defects']
        self.assertEqual(defect['issue_category'], DATA_TYPE_MISMATCH)

    def test_data_type_mismatch_string_field(self):
        report = run([sql('INV001', status='true')], [cosmos('INV001', status=True)])
        [defect] = report['defects']
        self.assertEqual(defect['issue_category'], DATA_TYPE_MISMATCH)

    def test_numeric_string_vs_number_is_not_a_type_defect(self):
        report = run([sql('INV001', amount='250.00')], [cosmos('INV001', amount=250)])
        self.assertEqual(report['validation_status'], 'PASS')

    def test_boolean_field_categories(self):
        mapping = {'key': {'source': 'Id', 'target': 'id'},
                   'fields': [{'source': 'IsActive', 'target': 'metadata.enabled', 'type': 'boolean'}]}
        source = [{'Id': 'B1', 'IsActive': 1}, {'Id': 'B2', 'IsActive': 'Y'}, {'Id': 'B3', 'IsActive': 'maybe'},
                  {'Id': 'B4', 'IsActive': None}]
        target = [{'id': 'B1', 'metadata': {'enabled': True}}, {'id': 'B2', 'metadata': {'enabled': False}},
                  {'id': 'B3', 'metadata': {'enabled': True}}, {'id': 'B4', 'metadata': {'enabled': False}}]
        report = build_defect_report(reconcile(source, target, mapping), mapping, source, target)
        by_key = {d['business_key']: d for d in report['defects']}
        self.assertEqual(sorted(by_key), ['B2', 'B3', 'B4'])  # 1 vs true is the same boolean
        self.assertEqual(by_key['B2']['issue_category'], FIELD_VALUE_MISMATCH)
        self.assertEqual((by_key['B2']['expected_value'], by_key['B2']['actual_value']), ('Y', False))
        self.assertIn('as boolean', by_key['B2']['expected_result'])
        self.assertTrue(by_key['B2']['business_impact'].startswith('Not determined'))
        self.assertEqual(by_key['B3']['issue_category'], DATA_TYPE_MISMATCH)
        self.assertEqual(by_key['B4']['issue_category'], NULL_VALUE_MISMATCH)
        for defect in report['defects']:
            self.assertComplete(defect)

    def test_duplicate_business_key_marks_incomplete_and_suppresses_comparison(self):
        source = [sql('INV001', amount='100.00'), sql('INV001', voucher='PRV009', amount='999.00')]
        target = [cosmos('INV001', amount=5)]
        report = run(source, target)
        self.assertEqual(report['validation_status'], 'INCOMPLETE')
        self.assertTrue(any('Duplicate business key' in r for r in report['incomplete_reasons']))
        [defect] = report['defects']
        self.assertComplete(defect)
        self.assertEqual(defect['issue_category'], DUPLICATE_BUSINESS_KEY)
        self.assertEqual(defect['actual_value'], 2)
        self.assertEqual(defect['voucher'], 'PRV001, PRV009')
        self.assertTrue(any('INV001' in s['item'] for s in report['suppressed']))

    def test_duplicate_does_not_suppress_other_keys(self):
        source = [sql('INV001'), sql('INV001'), sql('INV002', amount='10.00')]
        target = [cosmos('INV001'), cosmos('INV002', amount=11)]
        categories = [d['issue_category'] for d in run(source, target)['defects']]
        self.assertEqual(categories, [DUPLICATE_BUSINESS_KEY, FIELD_VALUE_MISMATCH])

    def test_missing_business_key_suppresses_extra_records(self):
        source = [sql('INV001'), {'Voucher': 'PRV004'}]
        target = [cosmos('INV001'), cosmos('INV004')]
        report = run(source, target)
        self.assertEqual(report['validation_status'], 'INCOMPLETE')
        self.assertEqual(report['defects'], [])
        self.assertTrue(any('extra-in-target for INV004' == s['item'] for s in report['suppressed']))

    def test_structurally_incomplete_result_generates_nothing(self):
        result = reconcile([sql('INV001')], [cosmos('INV001', amount=5)], MAPPING)
        result['summary']['source_count'] = 5
        report = build_defect_report(result, MAPPING)
        self.assertEqual(report['validation_status'], 'INCOMPLETE')
        self.assertEqual(report['defects'], [])
        report = build_defect_report({'summary': {}}, MAPPING)
        self.assertEqual(report['validation_status'], 'INCOMPLETE')

    def test_one_defect_per_field_difference(self):
        report = run([sql('INV001', amount='1.00', status='Approved')], [cosmos('INV001', amount=2, status='Rejected')])
        self.assertEqual(len(report['defects']), 2)
        self.assertEqual(len({d['defect_id'] for d in report['defects']}), 2)

    def test_works_without_raw_records(self):
        source, target = [sql('INV001', amount='1.00')], [cosmos('INV001', amount=2)]
        [defect] = generate_defect_drafts(reconcile(source, target, MAPPING), MAPPING)
        self.assertEqual(defect['voucher'], 'Not available')
        self.assertEqual(defect['expected_value'], '1.00')

    def test_csv_export(self):
        report = run([sql('INV001', amount='1.00')], [cosmos('INV001', amount=2), cosmos('INV004')])
        rows = list(csv.DictReader(io.StringIO(defects_to_csv(report['defects']))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['status'], STATUS)
        self.assertIn('1. ', rows[0]['steps_to_reproduce'])
        json.dumps(report)  # JSON download must serialize

    def test_bundled_sample_data(self):
        mapping = json.loads((ROOT / 'config' / 'mapping.json').read_text(encoding='utf-8'))
        source = json.loads((ROOT / 'data' / 'sql_sample.json').read_text(encoding='utf-8'))
        target = json.loads((ROOT / 'data' / 'cosmos_sample.json').read_text(encoding='utf-8'))
        report = build_defect_report(reconcile(source, target, mapping), mapping, source, target)
        self.assertEqual(report['validation_status'], 'FAIL')
        found = {(d['issue_category'], d['business_key']) for d in report['defects']}
        self.assertEqual(found, {(FIELD_VALUE_MISMATCH, 'INV001'), (MISSING_IN_TARGET, 'INV003'), (EXTRA_IN_TARGET, 'INV004')})

    def test_receivable_header_object_and_array_shapes(self):
        mapping = {'key': {'source': 'InvoiceNumber', 'target': 'ReceivableHeader.InvoiceNumber'},
                   'fields': [{'source': 'Memo', 'target': 'ReceivableHeader.Memo', 'type': 'string'}]}
        source = [{'InvoiceNumber': 'INV1', 'Memo': 'a'}, {'InvoiceNumber': 'INV2', 'Memo': 'b'},
                  {'InvoiceNumber': 'INV3', 'Memo': 'c'}]
        target = [{'ReceivableHeader': {'InvoiceNumber': 'INV1', 'Memo': 'a'}},
                  {'ReceivableHeader': [{'InvoiceNumber': 'INV2', 'Memo': 'changed'}]},
                  {'ReceivableHeader': [{'InvoiceNumber': 'INV3'}]}]
        report = build_defect_report(reconcile(source, target, mapping), mapping, source, target)
        by_key = {d['business_key']: d for d in report['defects']}
        self.assertEqual(set(by_key), {'INV2', 'INV3'})
        self.assertEqual(by_key['INV2']['issue_category'], FIELD_VALUE_MISMATCH)
        self.assertEqual(by_key['INV2']['actual_value'], 'changed')
        self.assertEqual(by_key['INV3']['issue_category'], NULL_VALUE_MISMATCH)
        self.assertIn('attribute absent', by_key['INV3']['actual_result'])

    def test_multi_item_array_is_data_type_mismatch(self):
        mapping = {'key': {'source': 'InvoiceNumber', 'target': 'InvoiceNumber'},
                   'fields': [{'source': 'Memo', 'target': 'ReceivableHeader.Memo', 'type': 'string'}]}
        source = [{'InvoiceNumber': 'INV1', 'Memo': 'a'}]
        target = [{'InvoiceNumber': 'INV1', 'ReceivableHeader': [{'Memo': 'a'}, {'Memo': 'b'}]}]
        [defect] = build_defect_report(reconcile(source, target, mapping), mapping, source, target)['defects']
        self.assertEqual(defect['issue_category'], DATA_TYPE_MISMATCH)
        self.assertEqual(defect['actual_value'], ['a', 'b'])

    def test_db_names_appear_in_description_and_steps(self):
        source = [sql('INV001', amount='1500.00'), sql('INV003'), sql('INV005'), sql('INV005')]
        target = [cosmos('INV001', amount=1400), cosmos('INV004'), cosmos('INV005')]
        context = {'source_name': 'FinanceERP', 'target_name': 'receivables-db',
                   'source_dataset': 'data/sql_sample.json', 'target_dataset': 'data/cosmos_sample.json'}
        report = build_defect_report(reconcile(source, target, MAPPING), MAPPING, source, target, context)
        by_category = {d['issue_category']: d for d in report['defects']}
        src, tgt = "Source DB 'FinanceERP'", "Target DB 'receivables-db'"

        mismatch = by_category[FIELD_VALUE_MISMATCH]
        self.assertTrue(mismatch['defect_description'].startswith(
            f"The record with InvoiceNumber 'INV001' is present in {src} and in {tgt}"))
        self.assertEqual(mismatch['steps_to_reproduce'], [
            f"Query {src} for the record with 'InvoiceNumber' = 'INV001': the record is present.",
            f"Query {tgt} for the record with 'invoice.number' = 'INV001': the record is present.",
            "Compare Source 'Amount' with Target 'payment.amount' as decimal.",
        ])

        missing = by_category[MISSING_IN_TARGET]
        self.assertTrue(missing['defect_description'].startswith(
            f"The record with InvoiceNumber 'INV003' is present in {src} but not in {tgt}"))
        self.assertIn('no record is returned', missing['steps_to_reproduce'][1])

        extra = by_category[EXTRA_IN_TARGET]
        self.assertTrue(extra['defect_description'].startswith(
            f"The record with InvoiceNumber 'INV004' is present in {tgt} but not in {src}"))

        duplicate = by_category[DUPLICATE_BUSINESS_KEY]
        self.assertIn(f'present 2 times in {src}', duplicate['defect_description'])

        # File paths never appear in the steps; the only precondition is the Azure portal login.
        steps = json.dumps([d['steps_to_reproduce'] for d in report['defects']])
        self.assertNotIn('.json', steps)
        for defect in report['defects']:
            self.assertEqual(defect['preconditions'], ['Log in to the Azure portal.'])

    def test_generic_db_wording_without_names(self):
        report = run([sql('INV001', amount='1.00')], [cosmos('INV001', amount=2)])
        [defect] = report['defects']
        self.assertIn('present in Source DB and in Target DB', defect['defect_description'])
        self.assertTrue(defect['steps_to_reproduce'][0].startswith('Query Source DB for the record'))
        self.assertEqual(defect['preconditions'], ['Log in to the Azure portal.'])

    def test_wording_follows_business_key_of_any_payload(self):
        mapping = {'key': {'source': 'Customer.CustomerId', 'target': 'CUSTOMERID'},
                   'fields': [{'source': 'Customer.Name', 'target': 'NAME', 'type': 'string'}]}
        source = [{'Customer': {'CustomerId': 'C100', 'Name': 'Acme'}}, {'Customer': {'CustomerId': 'C200', 'Name': 'Beta'}},
                  {'Customer': {'CustomerId': 'C300', 'Name': 'Dup'}}, {'Customer': {'CustomerId': 'C300', 'Name': 'Dup'}}]
        target = [{'CUSTOMERID': 'C100', 'NAME': 'Acme Ltd'}, {'CUSTOMERID': 'C300', 'NAME': 'Dup'},
                  {'CUSTOMERID': 'C900', 'NAME': 'Ghost'}]
        report = build_defect_report(reconcile(source, target, mapping), mapping, source, target)
        by_category = {d['issue_category']: d for d in report['defects']}
        self.assertEqual(set(by_category), {FIELD_VALUE_MISMATCH, MISSING_IN_TARGET, EXTRA_IN_TARGET, DUPLICATE_BUSINESS_KEY})
        mismatch = by_category[FIELD_VALUE_MISMATCH]
        self.assertEqual(mismatch['record_identifier'], 'CustomerId: C100')  # no voucher field mapped, so none shown
        self.assertTrue(mismatch['defect_description'].startswith("The record with CustomerId 'C100' is present in"))
        self.assertIn("exposes the record with CustomerId 'C900'", by_category[EXTRA_IN_TARGET]['business_impact'])
        # Neither the draft text nor the field names assume an invoice payload.
        self.assertNotIn('invoice', json.dumps([list(d.values()) for d in report['defects']]).lower())
        self.assertNotIn('invoice', ' '.join(key for d in report['defects'] for key in d).lower())
        self.assertEqual(mismatch['business_key'], 'C100')
        self.assertNotIn('invoice', defects_to_csv(report['defects']).lower())

    def test_nested_voucher_field_is_found(self):
        mapping = {'key': {'source': 'ReceivableHeader.InvoiceNumber', 'target': 'INVOICENUMBER'},
                   'fields': [{'source': 'ReceivableHeader.Voucher', 'target': 'LEDGERVOUCHER', 'type': 'string'},
                              {'source': 'ReceivableHeader.CustomerNumber', 'target': 'AVAINVOICEACCOUNT', 'type': 'string'}]}
        source = [{'ReceivableHeader': {'InvoiceNumber': 'INV1', 'Voucher': 'V1', 'CustomerNumber': 'Test05'}}]
        target = [{'INVOICENUMBER': 'INV1', 'LEDGERVOUCHER': 'V1', 'AVAINVOICEACCOUNT': 'Test01'}]
        [defect] = build_defect_report(reconcile(source, target, mapping), mapping, source, target)['defects']
        self.assertEqual(defect['voucher'], 'V1')
        self.assertEqual(defect['record_identifier'], 'InvoiceNumber: INV1 / Voucher: V1')

    def test_wording_uses_source_and_target_only(self):
        source = [sql('INV001', amount='1.00'), sql('INV002', status='Approved'), sql('INV003', amount='2.00'),
                  sql('INV004'), sql('INV005'), sql('INV005')]
        target = [cosmos('INV001', amount=5), cosmos('INV002', status=None), cosmos('INV003', amount='bad'),
                  cosmos('INV005'), cosmos('INV009')]
        report = run(source, target)
        categories = {d['issue_category'] for d in report['defects']}
        self.assertEqual(categories, {FIELD_VALUE_MISMATCH, NULL_VALUE_MISMATCH, DATA_TYPE_MISMATCH,
                                      MISSING_IN_TARGET, EXTRA_IN_TARGET, DUPLICATE_BUSINESS_KEY})
        text = json.dumps(report)
        for term in ('SQL Server', 'Cosmos', 'COSMOS'):
            self.assertNotIn(term, text)
        [mismatch] = [d for d in report['defects'] if d['issue_category'] == FIELD_VALUE_MISMATCH]
        self.assertIn("Source value: '1.00'. Target value: 5.", mismatch['defect_description'])

    def test_extreme_decimals_do_not_crash_and_variance_is_exact(self):
        report = run([sql('INV001', amount='-9E+999999')], [cosmos('INV001', amount='9E+999999')])
        [defect] = report['defects']
        self.assertIn('differs from Source by 1.8E+1000000 ', defect['business_impact'])
        report = run([sql('INV001', amount='1')], [cosmos('INV001', amount='1E+30')])
        [defect] = report['defects']
        self.assertIn(f"by {'9' * 30} ", defect['business_impact'])  # not rounded to 28 digits
        report = run([sql('INV001', amount='1')], [cosmos('INV001', amount='1E+999999')])
        [defect] = report['defects']
        self.assertIn('by approximately ', defect['business_impact'])

    def test_nan_and_infinity_are_not_numeric_mismatches(self):
        self.assertEqual(run([sql('INV001', amount='NaN')], [cosmos('INV001', amount='NaN')])['validation_status'], 'PASS')
        [defect] = run([sql('INV001', amount='Infinity')], [cosmos('INV001', amount=100)])['defects']
        self.assertEqual(defect['issue_category'], DATA_TYPE_MISMATCH)
        self.assertNotIn('variance', defect['business_impact'].lower())

    def test_absent_attribute_value_is_distinct_from_null(self):
        target = cosmos('INV001')
        del target['status']
        report = run([sql('INV001', status='Approved'), sql('INV002', status='Approved')],
                     [target, cosmos('INV002', status=None)])
        by_key = {d['business_key']: d for d in report['defects']}
        self.assertEqual(by_key['INV001']['actual_value'], '<attribute absent>')
        self.assertIsNone(by_key['INV002']['actual_value'])
        rows = {r['business_key']: r for r in csv.DictReader(io.StringIO(defects_to_csv(report['defects'])))}
        self.assertEqual(rows['INV001']['actual_value'], '<attribute absent>')
        self.assertEqual(rows['INV002']['actual_value'], 'NULL')

    def test_explicit_null_vs_absent_attribute_is_a_null_value_defect(self):
        target = cosmos('INV001')
        del target['status']
        source = [sql('INV001', status=None), sql('INV002', status=None), sql('INV003', status=None)]
        source[1].pop('ApprovalStatus')
        report = run(source, [target, cosmos('INV002', status=None), cosmos('INV003', status=None)])
        self.assertEqual(report['validation_status'], 'FAIL')
        by_key = {d['business_key']: d for d in report['defects']}
        self.assertEqual(sorted(by_key), ['INV001', 'INV002'])  # null vs null (INV003) is not a mismatch
        first, second = by_key['INV001'], by_key['INV002']
        self.assertComplete(first)
        self.assertEqual(first['issue_category'], NULL_VALUE_MISMATCH)
        self.assertEqual((first['expected_value'], first['actual_value']), (None, '<attribute absent>'))
        self.assertIn("Target 'status' is absent but Source 'ApprovalStatus' is an explicit null", first['defect_title'])
        self.assertIn('an explicit null on one side and absent on the other', first['defect_description'])
        self.assertIn("Target 'status' is <attribute absent>; Source 'ApprovalStatus' is NULL.", first['actual_result'])
        self.assertIn('serialization difference', first['probable_cause'])
        self.assertTrue(first['business_impact'].startswith('Not determined'))
        self.assertEqual((second['expected_value'], second['actual_value']), ('<attribute absent>', None))
        self.assertIn("Source 'ApprovalStatus' is absent but Target 'status' is an explicit null", second['defect_title'])
        rows = {r['business_key']: r for r in csv.DictReader(io.StringIO(defects_to_csv(report['defects'])))}
        self.assertEqual((rows['INV001']['expected_value'], rows['INV001']['actual_value']), ('NULL', '<attribute absent>'))

    def test_null_vs_absent_uses_reconcile_flags_without_the_records(self):
        target = cosmos('INV001')
        del target['status']
        result = reconcile([sql('INV001', status=None)], [target], MAPPING)
        [defect] = build_defect_report(result, MAPPING)['defects']
        self.assertEqual((defect['expected_value'], defect['actual_value']), (None, '<attribute absent>'))
        self.assertIn("Target 'status' is absent", defect['defect_title'])

    def test_transformed_null_vs_absent_attribute(self):
        source = [{'Id': 'P1', 'Qty': '1', 'Status': None, 'Name': 'n'}, {'Id': 'P2', 'Qty': '1', 'Name': 'n'}]
        target = [{'id': 'P1', 'qty': 100, 'name': 'n'}, {'id': 'P2', 'qty': 100, 'status': None, 'name': 'n'}]
        by_key = {d['business_key']: d for d in self.transform_report(source, target)['defects']}
        first, second = by_key['P1'], by_key['P2']
        self.assertEqual(first['issue_category'], NULL_VALUE_MISMATCH)
        self.assertEqual((first['source_value'], first['expected_value'], first['actual_value']),
                         (None, None, '<attribute absent>'))
        self.assertIn("Target 'status' is absent but transformed Source 'Status' is an explicit null", first['defect_title'])
        self.assertEqual((second['source_value'], second['expected_value'], second['actual_value']),
                         ('<attribute absent>', '<attribute absent>', None))
        self.assertIn("transformed Source 'Status' is absent but Target 'status' is an explicit null", second['defect_title'])

    def test_empty_array_on_path_is_absent_not_null(self):
        mapping = {'key': {'source': 'K', 'target': 'K'}, 'fields': [{'source': 'A', 'target': 'H.A', 'type': 'string'}]}
        source, target = [{'K': '1', 'A': 'x'}], [{'K': '1', 'H': []}]
        [defect] = build_defect_report(reconcile(source, target, mapping), mapping, source, target)['defects']
        self.assertIn("Target 'H.A' is <attribute absent>", defect['actual_result'])
        self.assertEqual(defect['actual_value'], '<attribute absent>')

    def test_csv_neutralises_formulas_but_keeps_plain_numbers(self):
        source = [sql('INV001', status='Approved', amount='-750.00')]
        target = [cosmos('INV001', status='=HYPERLINK("http://evil","x")', amount='@SUM(1)')]
        report = run(source, target)
        rows = list(csv.DictReader(io.StringIO(defects_to_csv(report['defects']))))
        by_field = {r['affected_field']: r for r in rows}
        status = by_field['ApprovalStatus (Source) -> status (Target)']
        self.assertEqual(status['actual_value'], '\'=HYPERLINK("http://evil","x")')
        amount = by_field['Amount (Source) -> payment.amount (Target)']
        self.assertEqual(amount['actual_value'], "'@SUM(1)")
        self.assertEqual(amount['expected_value'], '-750.00')  # a negative amount is not a formula
        for row in rows:
            for value in row.values():
                self.assertFalse(value.startswith(('=', '@')), value)

    TRANSFORM_MAPPING = {'key': {'source': 'Id', 'target': 'id'}, 'fields': [
        {'source': 'Qty', 'target': 'qty', 'type': 'decimal', 'transformation': 'multiply(100)'},
        {'source': 'Status', 'target': 'status', 'type': 'string', 'transformation': 'map(A=Active; I=Inactive)'},
        {'source': 'Name', 'target': 'name', 'type': 'string'}]}

    def transform_report(self, source, target):
        return build_defect_report(reconcile(source, target, self.TRANSFORM_MAPPING), self.TRANSFORM_MAPPING,
                                   source, target, {'source_name': 'abc', 'target_name': 'bcd'})

    def test_transformed_field_passes_when_target_holds_the_rule_result(self):
        report = self.transform_report([{'Id': 'P1', 'Qty': '1.5', 'Status': 'A', 'Name': 'n'}],
                                       [{'id': 'P1', 'qty': 150, 'status': 'Active', 'name': 'n'}])
        self.assertEqual(report['validation_status'], 'PASS')

    def test_transformed_field_draft_shows_source_rule_and_expected_value(self):
        report = self.transform_report([{'Id': 'P1', 'Qty': '1.5', 'Status': 'I', 'Name': 'n'}],
                                       [{'id': 'P1', 'qty': 155, 'status': 'Active', 'name': 'n'}])
        by_field = {d['affected_field']: d for d in report['defects']}
        qty = by_field['Qty (Source) -> qty (Target)']
        self.assertEqual(qty['issue_category'], FIELD_VALUE_MISMATCH)
        self.assertEqual((qty['source_value'], qty['transformation'], qty['expected_value'], qty['actual_value']),
                         ('1.5', 'multiply(100)', '150.0', 155))
        self.assertIn("transformed Source 'Qty'", qty['defect_title'])
        self.assertIn("Source value: '1.5'. Expected Target value after transformation: 150.0. Target value: 155.",
                      qty['defect_description'])
        self.assertTrue(qty['business_impact'].startswith(
            'Numeric variance observed: Target value differs from the transformed Source value by 5.0'))
        self.assertIn("Apply the STTM transformation 'multiply(100)'", qty['steps_to_reproduce'][2])
        self.assertIn('STTM transformation', qty['probable_cause'])
        status = by_field['Status (Source) -> status (Target)']
        self.assertEqual((status['source_value'], status['expected_value']), ('I', 'Inactive'))
        rows = list(csv.DictReader(io.StringIO(defects_to_csv(report['defects']))))
        self.assertEqual([r['transformation'] for r in rows], ['multiply(100)', 'map(A=Active; I=Inactive)'])

    def test_value_the_rule_cannot_process_is_a_data_type_defect(self):
        report = self.transform_report([{'Id': 'P1', 'Qty': 'abc', 'Status': 'A', 'Name': 'n'}],
                                       [{'id': 'P1', 'qty': 0, 'status': 'Active', 'name': 'n'}])
        [defect] = report['defects']
        self.assertEqual(defect['issue_category'], DATA_TYPE_MISMATCH)
        self.assertEqual(defect['expected_value'], '<transformation failed>')
        self.assertIn("multiply(100) needs a number, got 'abc'", defect['defect_description'])

    def test_fields_without_a_rule_keep_their_draft_shape(self):
        report = self.transform_report([{'Id': 'P1', 'Qty': '1', 'Status': 'A', 'Name': 'a'}],
                                       [{'id': 'P1', 'qty': 100, 'status': 'Active', 'name': 'b'}])
        [defect] = report['defects']
        self.assertNotIn('transformation', defect)
        self.assertNotIn('source_value', defect)
        self.assertEqual(defect['expected_value'], 'a')
        [row] = csv.DictReader(io.StringIO(defects_to_csv(report['defects'])))
        self.assertNotIn('source_value', row)  # no draft has a transformation, so no transformation columns
        self.assertNotIn('transformation', row)

    def test_voucher_only_when_the_mapping_has_a_voucher_field(self):
        # TRANSFORM_MAPPING has no Voucher field: neither the drafts nor the CSV mention a voucher.
        report = self.transform_report([{'Id': 'P1', 'Qty': '1', 'Status': 'A', 'Name': 'a'}],
                                       [{'id': 'P1', 'qty': 100, 'status': 'Active', 'name': 'b'}])
        [defect] = report['defects']
        self.assertNotIn('voucher', defect)
        self.assertEqual(defect['record_identifier'], 'Id: P1')
        header = defects_to_csv(report['defects']).splitlines()[0].split(',')
        self.assertNotIn('voucher', header)
        # MAPPING has a Voucher field: the column stays, with the value from the record.
        report = run([sql('INV001', amount='1.00')], [cosmos('INV001', amount=2)])
        [row] = csv.DictReader(io.StringIO(defects_to_csv(report['defects'])))
        self.assertEqual(row['voucher'], 'PRV001')


if __name__ == '__main__':
    unittest.main()
