# Source proxy + Target Proxy Validation & Defect Creation Agent

A read-only data validation agent that compares Source Proxy and Target Proxy records field by field using an STTM mapping, including transformation rules. It reports matches and mismatches, including missing or extra records, duplicate business keys, null and data-type differences. For each failure it drafts a defect with exact record IDs and evidence. It never modifies data, and no defect is filed until a user approves it.

## Requirements
Python 3.10+ and Claude Code installed. The sample needs no database credentials.

## Quick start (Windows PowerShell)
```powershell
cd db-validation-agent
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
python -m src.main --mode sample
claude
```
If Python 3.13 is unavailable, use `py -3.10` or `python` instead.

Inside Claude Code: `Use the db-validation-agent to validate the sample source and target records and explain all discrepancies.`

## Streamlit UI
```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```
The sidebar accepts optional Source/Target JSON files and an optional STTM mapping file (`.json` in the `config/mapping.json` format, or `.csv`/`.xlsx` with columns `source, target, type, key` and an optional `transformation` column; mark the business key row with `Y`). Without an upload, `config/mapping.json` is used. A CSV template can be downloaded from the sidebar.

Types: `string`, `decimal`, `date` or `boolean` (plus common SQL aliases such as `nvarchar`, `int`, `datetime2`, `bit`). A boolean field accepts `true`/`false`, `1`/`0`, `Y`/`N`, `yes`/`no` and `t`/`f` (any case), so a SQL bit `1` matches a JSON `true`; any other value is reported as a data type mismatch.

Nested fields: a row with only a Target name (e.g. `ReceivableHeader`, type blank/`object`/`array`) is a group row, and the field rows below it are read as `ReceivableHeader.<field>` until the next group row. A parent row may also name both sides with the type left blank (e.g. `ProductDetails` → `item`): the typed field rows below it are read as `ProductDetails.<field>` → `item.<field>` until the next parent row. A blank-type row that is not followed by typed field rows is an ordinary field compared as string. Target objects may be stored either as an object or as a single-item array; both are handled. An array with several items cannot match one flat source row and is reported as a data type mismatch (or a key issue when it holds the business key).

An explicit `null` and an absent attribute are different values. A mapped field that is `null` on one side and missing on the other (an empty array on the path counts as missing) is reported as a null value mismatch; `null` on both sides, or missing on both sides, passes. Fields that are not in the STTM are not compared.

### Transformations
When Target is derived from Source rather than copied, write the rule in the optional `transformation` column (also recognised as *Transformation Rule*, *Transformation Logic*, *Business Rule*, *Derivation*, *Rule* or *Logic*). The rule is applied to the Source value, and Target is compared with the result using the row's type. A blank cell, or `Direct`, `Direct move`, `As is`, `1:1`, `None`, `N/A` or `-`, means the value is copied as is. In a `.json` mapping, add `"transformation": "<rule>"` to a field.

Chain steps with `|`; they run left to right, e.g. `trim | upper` or `multiply(100) | round(2)`. Text arguments may be quoted (`'...'`), which keeps spaces, commas and `|`.

| Rule | Example | Source → expected Target |
|---|---|---|
| `upper`, `lower`, `trim`, `ltrim`, `rtrim` | `trim \| upper` | `' ball valve '` → `'BALL VALVE'` |
| `prefix(text)`, `suffix(text)` | `prefix('PRD-')` | `1001` → `'PRD-1001'` |
| `replace(old, new)` | `replace('-', '')` | `'12-34'` → `'1234'` |
| `left(n)`, `right(n)`, `substring(start, length)` | `substring(2, 3)` | `'ABCDEF'` → `'BCD'` (start is 1-based) |
| `lpad(width, char)`, `rpad(width, char)` | `lpad(6, '0')` | `42` → `'000042'` |
| `map(code=value; ...; *=default)` | `map(A=Active; I=Inactive; *=Unknown)` | `'I'` → `'Inactive'`; a code with no entry and no `*` is kept unchanged |
| `multiply(n)`, `divide(n)`, `add(n)`, `subtract(n)`, `round(digits)`, `abs`, `negate` | `multiply(100) \| round(0)` | `'12.345'` → `1235` (exact decimals; round is half away from zero, like SQL `ROUND`) |
| `multiply(field)`, `divide(field)`, `add(field)`, `subtract(field)` | `multiply(AdjustmentDetails.Quantity)` on `UnitCost` | `250.5` × `150` → `37575.0`; a null field gives null |
| `default(value)` (also `nvl`, `ifnull`, `isnull`) | `default('N/A')` | null or absent → `'N/A'` (an empty string is not null) |
| `coalesce(a, b, ...)` | `coalesce(MobilePhone, HomePhone, 'none')` | first non-null of the named Source fields / quoted literals |
| `concat(a, b, ...)` | `concat(FirstName, ' ', LastName)` | `'Ann Lee'`; a null part counts as empty, like SQL `CONCAT` |
| `to_date(format)` | `to_date(DD/MM/YYYY)` | `'05/11/2026'` → `2026-11-05` |
| `format_date(format)` | `format_date(DD-MON-YYYY)` | `'2026-11-05'` → `'05-Nov-2026'` |

- In `concat`, `coalesce` and the arithmetic rules, an unquoted name is a Source field, looked up next to the mapped Source field first (same parent object, e.g. `Customer.LastName` for a row under `Customer`) and then from the top of the record; `value` is the mapped field's own value.
- Date formats use `YYYY`, `YY`, `MM`, `DD`, `MON`, `HH`/`HH24`, `MI` (minutes) and `SS`, or Python `%` codes.
- A null Source value stays null, and an absent one stays absent, through every rule except `default`, `coalesce` and `concat`. Target must match that: `null` where the rule gives null, no attribute where it leaves the attribute absent.
- A Source value the rule cannot process (e.g. `multiply(100)` on `'abc'`, or a date in a different format) is reported as a data type mismatch with the reason.
- Anything else in the column, e.g. a sentence like *"convert to uppercase"*, stops the run with the STTM row number and the list of supported rules, so a field is never compared without its rule. Rewrite the cell as a rule, or leave the row out of the STTM.
- The business key row cannot have a transformation: records are matched on the key values as they are.
- Defect drafts for a transformed field show the raw `source_value`, the `transformation`, the `expected_value` after the rule and the Target `actual_value`. These two extra columns are at the end of the defect CSV, only when at least one field has a transformation, and are empty for other fields. Likewise, the `voucher` field and CSV column appear only when the STTM maps a field named `Voucher`.

## Command line / Claude Code
```powershell
python -m src.main --mode sample --source-file <source.json> --target-file <target.json> --mapping <sttm.json|csv|xlsx> --source-name "<Source DB name>" --target-name "<Target DB name>"
python -m src.sttm STTM.xlsx --out config/mapping.json   # convert STTM to the default mapping (backs up the old one)
```
Results go to `reports/validation_report.json`, `reports/validation_report.csv` and `reports/defect_drafts.json`. The CSV is the row-by-row comparison: one row per mapped field of every record, with the business key, Source and Target field, Source value, STTM transformation and Expected Target value (for a transformed field), Target value, result (`MATCH`, `MISMATCH`, `MISSING IN TARGET`, `EXTRA IN TARGET`, or `KEY ISSUE` with the reason) and the defect draft raised for it. An explicit null is shown as `NULL` and an absent attribute as `<attribute absent>`; a record found on one side only shows `<no Source record>` or `<no Target record>` on the other. The same rows are in the JSON report under `comparisons`. The UI's validation results don't repeat every field: under the Passed / Mismatched / Missing / Extra / Key issues counts, a table lists what passed and what failed, with the columns key, status, source_field, target_field, source_value and target_value (plus transformation and expected_value when the STTM has rules). It has one `PASS` row per record where every mapped field matches, one `FAIL` row per field that doesn't, and one `MISSING IN TARGET` / `EXTRA IN TARGET` row per record found on one side only. The row-by-row comparison of every field is the CSV, which the UI also offers as a download. The command also prints one line per draft (`id | business key | category | field | expected | actual`, plus the Source value and rule for a transformed field; values longer than 60 characters, or 120 for a rule, are cut and marked `[truncated, N chars]`), so Claude Code can report a run without reading the full JSON. `--source-name` / `--target-name` are optional; when given, defect descriptions and steps to reproduce name the databases where each record is (or is not) present, e.g. *"The record with InvoiceNumber 'INV001' is present in Source DB 'FinanceERP' and in Target DB 'receivables-db'"* (records are named after the Source business key field, so any payload works). In Claude Code, give the db-validation-agent the file paths and DB names, e.g. *"Use the db-validation-agent to validate C:\data\source.json against C:\data\target.json using C:\data\STTM.xlsx. Source DB is FinanceERP, target Cosmos DB is receivables-db"*. Or use the `/validate-db` skill, which runs the same engine: `/validate-db "C:\data\source.json" "C:\data\target.json" "C:\data\STTM.xlsx" FinanceERP receivables-db` (add `no-ui` for a chat-only result). The old `--sql-file`/`--cosmos-file` flags still work. Add `--open-ui` to open that run in the browser at `http://localhost:8501/?run=last`: a results-only page with just the validation results and defect drafts from the saved reports, including the DB names. Open `http://localhost:8501` without `?run=last` for the interactive page with uploads.

The Streamlit sidebar's **Save as default mapping** button does the same conversion for an uploaded STTM file.

## Live mode (opt-in)
Source and Target are both Cosmos DB. Install `pip install -r requirements.txt`, securely set the `SOURCE_COSMOS_*` and `TARGET_COSMOS_*` environment variables listed in `.env.example`, then create `config/source_query.sql` and `config/target_query.sql` with bounded read-only Cosmos SQL queries. Example Source query: `SELECT TOP 100 c.InvoiceNumber, c.Voucher, c.Amount, c.ApprovalStatus, c.DueDate FROM c WHERE c.InvoiceNumber IN ('INV001','INV002')`. Example Target query: `SELECT c.invoice, c.voucher, c.payment, c.status FROM c WHERE c.invoice.number IN ('INV001','INV002')`. Replace example schema with your actual schema. Review both queries and access permissions before running:

```powershell
python -m src.main --mode live --source-query-file config/source_query.sql --target-query-file config/target_query.sql
```

Note: `python-dotenv` is optional; this sample reads environment variables already set in your shell. Do not commit credentials, actual customer records, or production exports. Use a least-privilege read-only identity and restrict query size. For large datasets, implement pagination and streaming before production use. The validator flags duplicates but does not compare all duplicated rows.
