import argparse
import json
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from .validator import reconcile
from .comparison_report import comparison_csv, comparison_rows
from .defect_generator import build_defect_report
from .sttm import parse_sttm

ROOT = Path(__file__).resolve().parent.parent
LAST_RUN = ROOT / 'reports' / 'last_run.json'
UI_PORT = 8501


def ui_running():
    try:
        with urllib.request.urlopen(f'http://localhost:{UI_PORT}/_stcore/health', timeout=2) as response:
            return response.read().strip() == b'ok'
    except OSError:
        return False


def open_ui():
    """Start the Streamlit app on localhost if needed and open it on the last CLI run."""
    if not ui_running():
        log = open(ROOT / 'reports' / 'streamlit.log', 'w', encoding='utf-8')
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == 'win32' else 0
        subprocess.Popen([sys.executable, '-m', 'streamlit', 'run', str(ROOT / 'app.py'), '--server.headless', 'true',
                          '--server.address', 'localhost', '--server.port', str(UI_PORT)],
                         cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, creationflags=flags, start_new_session=sys.platform != 'win32')
        for _ in range(60):
            if ui_running():
                break
            time.sleep(0.5)
        else:
            print(f"UI did not start within 30 seconds; see {ROOT / 'reports' / 'streamlit.log'}")
            return
    url = f'http://localhost:{UI_PORT}/?run=last'
    webbrowser.open(url)
    print(f'UI: {url}')


SUMMARY_VALUE_LIMIT = 60
SUMMARY_RULE_LIMIT = 120


def _short(value, limit=SUMMARY_VALUE_LIMIT):
    """JSON-encode a draft value for a summary line: null, numbers and strings stay distinct,
    and newlines or '|' inside a value can't break the line. Long values are cut and marked."""
    text = json.dumps(value, default=str, ensure_ascii=False)
    if len(text) > limit:
        text = f'{text[:limit - 3]}... [truncated, {len(text)} chars]'
    return text


def draft_summary_line(draft):
    """One line per draft: id | business key | category | field | expected | actual.
    A draft for a transformed field also gets the raw Source value and the STTM rule."""
    parts = [draft['defect_id'], _short(draft['business_key']), draft['issue_category'], draft['affected_field'],
             f"expected {_short(draft['expected_value'])}", f"actual {_short(draft['actual_value'])}"]
    if 'transformation' in draft:
        parts.append(f"source {_short(draft.get('source_value'))} rule {_short(draft['transformation'], SUMMARY_RULE_LIMIT)}")
    return ' | '.join(parts)


def _emit(line):
    try:
        print(line)
    except UnicodeEncodeError:  # a console that can't show a character in the data
        print(line.encode('ascii', 'backslashreplace').decode('ascii'))


def load_records(path, label):
    records = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ValueError(f'{label} file {path} must be a JSON array of objects')
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['sample', 'live'], default='sample')
    parser.add_argument('--mapping', default='config/mapping.json', help='STTM mapping (.json, .csv or .xlsx)')
    # The --sql-* / --cosmos-* spellings are kept as aliases for existing scripts.
    parser.add_argument('--source-file', '--sql-file', dest='source_file', default='data/sql_sample.json')
    parser.add_argument('--target-file', '--cosmos-file', dest='target_file', default='data/cosmos_sample.json')
    parser.add_argument('--source-query-file', '--sql-query-file', dest='source_query_file')
    parser.add_argument('--target-query-file', '--cosmos-query-file', dest='target_query_file')
    parser.add_argument('--source-name', help='Exact Source DB name to use in defect drafts (e.g. FinanceERP)')
    parser.add_argument('--target-name', help='Exact Target (Cosmos) DB name to use in defect drafts (e.g. receivables-db)')
    parser.add_argument('--report', default='reports/validation_report.json')
    parser.add_argument('--defects-report', default='reports/defect_drafts.json')
    parser.add_argument('--open-ui', action='store_true', help='Open the Streamlit UI on this run after validating (sample mode)')
    args = parser.parse_args()
    if args.open_ui and args.mode != 'sample':
        parser.error('--open-ui is only available in sample mode')
    try:
        mapping = parse_sttm(Path(args.mapping).read_bytes(), args.mapping)
        if args.mode == 'sample':
            source = load_records(args.source_file, 'Source')
            target = load_records(args.target_file, 'Target')
            source_label, target_label = args.source_file, args.target_file
        else:
            if not args.source_query_file or not args.target_query_file:
                parser.error('Live mode requires both --source-query-file and --target-query-file')
            from .connectors import read_cosmos
            source = read_cosmos(Path(args.source_query_file).read_text(), 'SOURCE')
            target = read_cosmos(Path(args.target_query_file).read_text(), 'TARGET')
            source_label = f'live query {args.source_query_file}'
            target_label = f'live query {args.target_query_file}'
    except (OSError, ValueError, UnicodeError) as exc:
        parser.error(str(exc))
    result = reconcile(source, target, mapping)
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=str))
    defect_report = build_defect_report(result, mapping, source, target, {
        'source_dataset': source_label, 'target_dataset': target_label, 'mapping_file': args.mapping,
        'source_name': args.source_name, 'target_name': args.target_name})
    defects_output = Path(args.defects_report)
    defects_output.parent.mkdir(parents=True, exist_ok=True)
    defects_output.write_text(json.dumps(defect_report, indent=2, default=str))
    # Row-by-row comparison next to the JSON report; utf-8-sig so Excel reads non-ASCII values correctly.
    comparison_output = (output.with_suffix('.csv') if output.suffix.lower() != '.csv'
                         else output.with_name(f'{output.stem}_comparison.csv'))
    comparison_output.write_text(comparison_csv(comparison_rows(result, defect_report)[0]), encoding='utf-8-sig')
    print(json.dumps(result['summary'], indent=2))
    print(f"Validation status: {defect_report['validation_status']} ({len(defect_report['defects'])} defect draft(s))")
    for anomaly in defect_report['anomalies']:
        _emit(f'ATTENTION REQUIRED: {anomaly}')
    for reason in defect_report['incomplete_reasons']:
        _emit(f'INCOMPLETE: {reason}')
    if defect_report['suppressed']:
        print(f"Suppressed findings: {len(defect_report['suppressed'])} (listed in {defects_output})")
    if defect_report['defects']:
        print('Drafts (id | business key | category | field | expected | actual [| source, rule]):')
        for draft in defect_report['defects']:
            _emit(f'  {draft_summary_line(draft)}')
    print(f'Report: {output}')
    print(f'Row-by-row comparison: {comparison_output}')
    print(f'Defect drafts: {defects_output}')
    if args.mode == 'sample':
        # Lets the UI show exactly this run's results via ?run=last.
        LAST_RUN.parent.mkdir(parents=True, exist_ok=True)
        LAST_RUN.write_text(json.dumps({
            'timestamp': datetime.now().isoformat(timespec='seconds'),
            'source_file': str(Path(args.source_file).resolve()),
            'target_file': str(Path(args.target_file).resolve()),
            'mapping_file': str(Path(args.mapping).resolve()),
            'source_name': args.source_name,
            'target_name': args.target_name,
            'validation_report': str(output.resolve()),
            'defects_report': str(defects_output.resolve()),
        }, indent=2))
    if args.open_ui:
        open_ui()
    return 1 if any(result['summary'][k] for k in ('mismatched','missing_in_target','extra_in_target','key_issues')) else 0


if __name__ == '__main__':
    raise SystemExit(main())
