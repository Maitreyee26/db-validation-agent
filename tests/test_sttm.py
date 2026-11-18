import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

from src.sttm import CSV_TEMPLATE, main as sttm_main, parse_sttm, save_mapping
from src.validator import reconcile

ROOT = Path(__file__).resolve().parent.parent


class SttmTests(unittest.TestCase):
    def setUp(self):
        self.expected = json.loads((ROOT / 'config' / 'mapping.json').read_text(encoding='utf-8'))

    def test_json_mapping(self):
        data = (ROOT / 'config' / 'mapping.json').read_bytes()
        self.assertEqual(parse_sttm(data, 'mapping.json'), self.expected)

    def test_csv_template_matches_default_mapping(self):
        self.assertEqual(parse_sttm(CSV_TEMPLATE.encode('utf-8'), 'sttm.csv'), self.expected)

    def test_xlsx_with_alias_headers_and_sql_types(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['Source Field', 'Target Field', 'Data Type', 'Business Key'])
        sheet.append(['InvoiceNumber', 'invoice.number', 'NVARCHAR(20)', 'Yes'])
        sheet.append(['Amount', 'payment.amount', 'DECIMAL(18,2)', None])
        sheet.append(['DueDate', 'payment.dueDate', 'datetime2', None])
        sheet.append([None, None, None, None])
        buffer = io.BytesIO()
        workbook.save(buffer)
        mapping = parse_sttm(buffer.getvalue(), 'STTM.xlsx')
        self.assertEqual(mapping['key'], {'source': 'InvoiceNumber', 'target': 'invoice.number'})
        self.assertEqual([f['type'] for f in mapping['fields']], ['string', 'decimal', 'date'])

    def test_parsed_mapping_drives_reconciliation(self):
        mapping = parse_sttm(CSV_TEMPLATE.encode('utf-8'), 'sttm.csv')
        source = json.loads((ROOT / 'data' / 'sql_sample.json').read_text(encoding='utf-8'))
        target = json.loads((ROOT / 'data' / 'cosmos_sample.json').read_text(encoding='utf-8'))
        self.assertEqual(reconcile(source, target, mapping), reconcile(source, target, self.expected))

    def test_group_row_prefixes_following_target_fields(self):
        sheet = (b'source,target,type,key\n'
                 b',ReceivableHeader,,\n'
                 b'InvoiceNumber,InvoiceNumber,string,Y\n'
                 b'CustomerNumber,CustomerNumber,string,\n'
                 b'InvoiceDate,ReceivableHeader.InvoiceDate,date,\n'
                 b',Payment,object,\n'
                 b'Amount,Amount,decimal,\n')
        mapping = parse_sttm(sheet, 'sttm.csv')
        self.assertEqual(mapping['key'], {'source': 'InvoiceNumber', 'target': 'ReceivableHeader.InvoiceNumber'})
        self.assertEqual([f['target'] for f in mapping['fields']], [
            'ReceivableHeader.InvoiceNumber', 'ReceivableHeader.CustomerNumber',
            'ReceivableHeader.InvoiceDate', 'Payment.Amount'])
        self.assertEqual([f['source'] for f in mapping['fields']], ['InvoiceNumber', 'CustomerNumber', 'InvoiceDate', 'Amount'])

    def test_group_row_in_excel(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['Source', 'Target', 'Type', 'Key'])
        sheet.append([None, 'ReceivableHeader', None, None])
        sheet.append(['InvoiceNumber', 'InvoiceNumber', 'string', 'Y'])
        sheet.append(['Memo', 'Memo', 'string', None])
        buffer = io.BytesIO()
        workbook.save(buffer)
        mapping = parse_sttm(buffer.getvalue(), 'STTM.xlsx')
        self.assertEqual([f['target'] for f in mapping['fields']], ['ReceivableHeader.InvoiceNumber', 'ReceivableHeader.Memo'])

    def test_parent_row_naming_both_sides_with_blank_type(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['source', 'target', 'type', 'key'])
        sheet.append(['ProductDetails', 'item', None, None])
        sheet.append(['ProductCode', 'itemId', 'string', 'Y'])
        sheet.append(['ProductName', 'description', 'string', None])
        sheet.append(['AdjustmentDetails', 'stockMovement', None, None])
        sheet.append(['Quantity', 'adjustedQuantity', 'integer', None])
        sheet.append(['AuditDetails', 'metadata', 'object', None])
        sheet.append(['CreatedBy', 'userId', 'string', None])
        buffer = io.BytesIO()
        workbook.save(buffer)
        mapping = parse_sttm(buffer.getvalue(), 'STTM.xlsx')
        self.assertEqual(mapping['key'], {'source': 'ProductDetails.ProductCode', 'target': 'item.itemId'})
        self.assertEqual([(f['source'], f['target'], f['type']) for f in mapping['fields']], [
            ('ProductDetails.ProductCode', 'item.itemId', 'string'),
            ('ProductDetails.ProductName', 'item.description', 'string'),
            ('AdjustmentDetails.Quantity', 'stockMovement.adjustedQuantity', 'decimal'),
            ('AuditDetails.CreatedBy', 'metadata.userId', 'string')])

    def test_blank_type_row_without_typed_fields_below_is_a_field(self):
        # A top-level field followed by a one-sided group row, as in sttm_template.xlsx.
        sheet = (b'source,target,type,key\n'
                 b'SourceSystem,SourceSystem,,\n'
                 b'ReceivableHeader,,,\n'
                 b'InvoiceNumber,INVOICENUMBER,string,Y\n'
                 b'Memo,MEMO,,\n')
        mapping = parse_sttm(sheet, 'sttm.csv')
        self.assertEqual([(f['source'], f['target'], f['type']) for f in mapping['fields']], [
            ('SourceSystem', 'SourceSystem', 'string'),
            ('ReceivableHeader.InvoiceNumber', 'INVOICENUMBER', 'string'),
            ('ReceivableHeader.Memo', 'MEMO', 'string')])

    def test_parent_rows_drive_reconciliation_of_nested_records(self):
        sheet = (b'source,target,type,key\n'
                 b'ProductDetails,item,,\n'
                 b'ProductCode,itemId,string,Y\n'
                 b'AdjustmentDetails,stockMovement,,\n'
                 b'Quantity,adjustedQuantity,integer,\n')
        mapping = parse_sttm(sheet, 'sttm.csv')
        source = [{'ProductDetails': {'ProductCode': 'P1'}, 'AdjustmentDetails': {'Quantity': 150}}]
        target = [{'item': {'itemId': 'P1'}, 'stockMovement': {'adjustedQuantity': 155}}]
        result = json.dumps(reconcile(source, target, mapping), default=str)
        self.assertIn('AdjustmentDetails.Quantity', result)
        self.assertIn('155', result)

    def test_typed_one_sided_row_is_still_an_error(self):
        with self.assertRaisesRegex(ValueError, 'row 3'):
            parse_sttm(b'source,target,type,key\nA,a,string,Y\n,b,string,\n', 'sttm.csv')

    def test_rejects_missing_key_row(self):
        with self.assertRaisesRegex(ValueError, 'exactly one business key'):
            parse_sttm(b'source,target,type,key\nA,a,string,\n', 'sttm.csv')

    def test_rejects_multiple_key_rows(self):
        with self.assertRaisesRegex(ValueError, 'exactly one business key'):
            parse_sttm(b'source,target,type,key\nA,a,string,Y\nB,b,string,Y\n', 'sttm.csv')

    def test_rejects_missing_columns(self):
        with self.assertRaisesRegex(ValueError, 'missing column'):
            parse_sttm(b'source,type\nA,string\n', 'sttm.csv')

    def test_boolean_type_and_aliases(self):
        mapping = parse_sttm(b'source,target,type,key\nId,id,string,Y\nIsActive,enabled,boolean,\n'
                             b'Flag,flag,BIT,\nOpen,open,bool,\n', 'sttm.csv')
        self.assertEqual([f['type'] for f in mapping['fields']], ['string', 'boolean', 'boolean', 'boolean'])

    def test_rejects_unknown_type(self):
        with self.assertRaisesRegex(ValueError, 'unsupported type'):
            parse_sttm(b'source,target,type,key\nA,a,geography,Y\n', 'sttm.csv')

    def test_rejects_incomplete_row(self):
        with self.assertRaisesRegex(ValueError, 'row 3'):
            parse_sttm(b'source,target,type,key\nA,a,string,Y\nB,,string,\n', 'sttm.csv')

    def test_rejects_invalid_json_mapping(self):
        with self.assertRaisesRegex(ValueError, "'key' and 'fields'"):
            parse_sttm(b'{"fields": []}', 'mapping.json')

    def test_save_mapping_backs_up_previous_version(self):
        out = Path(tempfile.mkdtemp()) / 'mapping.json'
        self.assertIsNone(save_mapping(self.expected, out))
        self.assertEqual(json.loads(out.read_text()), self.expected)
        self.assertIsNone(save_mapping(self.expected, out))  # unchanged content: no backup
        changed = parse_sttm(b'source,target,type,key\nId,id,string,Y\n', 'sttm.csv')
        backup = save_mapping(changed, out)
        self.assertEqual(json.loads(backup.read_text()), self.expected)
        self.assertEqual(json.loads(out.read_text()), changed)

    def test_convert_command(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'sttm.csv').write_text(CSV_TEMPLATE, encoding='utf-8')
        argv = ['src.sttm', str(tmp / 'sttm.csv'), '--out', str(tmp / 'mapping.json')]
        with mock.patch('sys.argv', argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sttm_main(), 0)
        self.assertEqual(json.loads((tmp / 'mapping.json').read_text()), self.expected)

    def test_transformation_column_in_excel(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['Source Field', 'Target Field', 'Data Type', 'Key', 'Transformation Rule'])
        sheet.append(['ProductDetails', 'item', None, None, None])
        sheet.append(['ProductCode', 'itemId', 'string', 'Y', 'Direct'])
        sheet.append(['ProductName', 'description', 'string', None, 'trim | upper'])
        sheet.append(['Quantity', 'quantity', 'decimal', None, 'multiply(100)'])
        sheet.append(['FullName', 'fullName', None, None, "concat(First, ' ', Last)"])  # blank type: still a field
        sheet.append(['Memo', 'memo', 'string', None, 'As is'])
        buffer = io.BytesIO()
        workbook.save(buffer)
        mapping = parse_sttm(buffer.getvalue(), 'STTM.xlsx')
        self.assertEqual([(f['source'], f.get('transformation')) for f in mapping['fields']], [
            ('ProductDetails.ProductCode', None), ('ProductDetails.ProductName', 'trim | upper'),
            ('ProductDetails.Quantity', 'multiply(100)'), ('ProductDetails.FullName', "concat(First, ' ', Last)"),
            ('ProductDetails.Memo', None)])
        self.assertEqual(mapping['fields'][3]['type'], 'string')

    def test_rule_row_counts_as_a_field_below_a_parent_row(self):
        sheet = (b'source,target,type,key,transformation\n'
                 b'Customer,customer,,,\n'
                 b'Id,id,string,Y,\n'
                 b'Name,name,,,upper\n')
        mapping = parse_sttm(sheet, 'sttm.csv')
        self.assertEqual([(f['source'], f['target']) for f in mapping['fields']],
                         [('Customer.Id', 'customer.id'), ('Customer.Name', 'customer.name')])

    def test_unknown_transformation_names_the_row_and_supported_rules(self):
        with self.assertRaisesRegex(ValueError, r"STTM row 3: invalid transformation 'Convert to uppercase'.*Supported: upper"):
            parse_sttm(b'source,target,type,key,transformation\nA,a,string,Y,\nB,b,string,,Convert to uppercase\n', 'sttm.csv')

    def test_transformation_on_business_key_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'STTM row 2: a transformation on the business key is not supported'):
            parse_sttm(b'source,target,type,key,transformation\nA,a,string,Y,trim\nB,b,string,,\n', 'sttm.csv')
        mapping = {'key': {'source': 'A', 'target': 'a'},
                   'fields': [{'source': 'A', 'target': 'a', 'type': 'string', 'transformation': 'trim'}]}
        with self.assertRaisesRegex(ValueError, 'business key'):
            parse_sttm(json.dumps(mapping).encode(), 'mapping.json')

    def test_json_mapping_transformation_is_validated(self):
        mapping = {'key': {'source': 'A', 'target': 'a'},
                   'fields': [{'source': 'B', 'target': 'b', 'type': 'decimal', 'transformation': 'times(2)'}]}
        with self.assertRaisesRegex(ValueError, "Mapping field 1: invalid transformation 'times\\(2\\)'"):
            parse_sttm(json.dumps(mapping).encode(), 'mapping.json')
        mapping['fields'][0]['transformation'] = 'Direct'
        self.assertNotIn('transformation', parse_sttm(json.dumps(mapping).encode(), 'mapping.json')['fields'][0])

    def test_rejects_corrupt_workbook_and_unknown_extension(self):
        with self.assertRaisesRegex(ValueError, 'Unable to read'):
            parse_sttm(b'not a workbook', 'sttm.xlsx')
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            parse_sttm(b'', 'sttm.txt')


if __name__ == '__main__':
    unittest.main()
