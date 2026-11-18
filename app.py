"""Streamlit UI for the Source/Target reconciliation engine.

Opened at /?run=last (what `python -m src.main --open-ui` opens), it shows only the validation
results and defect drafts of the latest command-line run. Opened without it, it is the
interactive sample-mode page with uploads.
"""
import csv
import io
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from src.comparison_report import comparison_columns, comparison_csv, comparison_rows
from src.validator import EXTRA_IN_TARGET, MATCH, MISMATCH, MISSING_IN_TARGET, reconcile
from src.defect_generator import build_defect_report, defects_to_csv
from src.sttm import CSV_TEMPLATE, parse_sttm, save_mapping, validate_mapping

ROOT = Path(__file__).resolve().parent

st.set_page_config(page_title="Source ↔ Target Validation", page_icon="🔎", layout="wide")


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse_records(upload, default_path):
    if upload is None:
        value = load_json(default_path)
    else:
        value = json.load(upload)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("Dataset must be a JSON array of objects")
    return value


RESULT_COLUMNS = ["key", "status", "source_field", "target_field", "source_value", "target_value"]
TRANSFORMATION_COLUMNS = ["transformation", "expected_value"]
PASS, FAIL = "PASS", "FAIL"
# Translucent tints read on both the light and the dark Streamlit theme.
PASS_TINT = "background-color: rgba(33, 195, 84, 0.16)"
FAIL_TINT = "background-color: rgba(255, 75, 75, 0.14)"
STYLE_ROW_LIMIT = 5000  # styling every cell of a very large table makes the page slow


def results_table(result, rows):
    """What passed and what didn't, in record order: one PASS row per record where every mapped field
    matches, and one row per field that doesn't (FAIL), per record on one side only (MISSING IN TARGET /
    EXTRA IN TARGET) and per key issue. The full row-by-row comparison is the CSV download."""
    passed = [str(key) for key in result.get("passed_keys", [])]
    passed_set, listed, table = set(passed), set(), []
    for row in rows:
        key, status = row["key"], row["result"]
        if status in (MISSING_IN_TARGET, EXTRA_IN_TARGET):
            # A record on one side only is one row; its fields are in the CSV.
            if (key, status) not in listed:
                listed.add((key, status))
                table.append({"key": key, "status": status})
        elif status != MATCH:
            table.append({**row, "status": FAIL if status == MISMATCH else status})
        elif key in passed_set and key not in listed:
            listed.add(key)
            table.append({"key": key, "status": PASS})
    # A report written before row-by-row comparisons were kept has no rows for passed records.
    table += [{"key": key, "status": PASS} for key in passed if key not in listed]
    with_rules = any(row.get("transformation") for row in table)
    return pd.DataFrame(table, columns=RESULT_COLUMNS + (TRANSFORMATION_COLUMNS if with_rules else [])).fillna("")


def tint_status(row):
    return [(PASS_TINT if row["status"] == PASS else FAIL_TINT) if column == "status" else "" for column in row.index]


def db_label(side, name):
    return f"{side} DB '{name}'" if name else f"{side} DB"


def render_validation(result, defect_report=None):
    st.subheader("Validation results")
    summary = result["summary"]
    metrics = st.columns(5)
    for col, label, key in zip(metrics, ["Passed", "Mismatched", "Missing in target", "Extra in target", "Key issues"], ["passed", "mismatched", "missing_in_target", "extra_in_target", "key_issues"]):
        col.metric(label, summary[key])

    rows, complete = comparison_rows(result, defect_report)
    columns = comparison_columns(rows, with_defects=defect_report is not None)
    table = results_table(result, rows)
    note = ("PASS: every mapped field of the record matches. FAIL: one row per field that doesn't match. "
            "MISSING IN TARGET / EXTRA IN TARGET: the record is on one side only. "
            "The row-by-row comparison of every field of every record is in the CSV download below.")
    if TRANSFORMATION_COLUMNS[0] in table.columns:
        note += (" For a field with an STTM transformation, expected_value is the rule applied to source_value, "
                 "and target_value is compared with it.")
    st.caption(note)
    if not complete:
        st.info("This report was written before row-by-row comparisons were kept, so the CSV lists only the "
                "fields that differ. Run the validation again to get every field.")
    shown = table.style.apply(tint_status, axis=1) if len(table) <= STYLE_ROW_LIMIT else table
    st.dataframe(shown, use_container_width=True, hide_index=True)

    with st.expander("Raw reconciliation result"):
        st.json(result)
    downloads = st.columns(2)
    downloads[0].download_button("Download row-by-row comparison of all records (CSV)", data=comparison_csv(rows, columns).encode("utf-8-sig"),
                                 file_name="validation_report.csv", mime="text/csv", use_container_width=True)
    downloads[1].download_button("Download JSON report", data=json.dumps(result, indent=2, default=str),
                                 file_name="validation_report.json", mime="application/json", use_container_width=True)


def render_defects(defect_report):
    st.subheader("Generated Defect Drafts")
    st.caption("Drafts are built from the reconciliation result above and are not submitted to any defect tracker.")
    defects = defect_report["defects"]

    if defect_report["validation_status"] == "INCOMPLETE":
        st.error("Validation status: **INCOMPLETE** — results are not fully reliable.")
        for reason in defect_report["incomplete_reasons"]:
            st.write(f"- {reason}")
        if defect_report["suppressed"]:
            with st.expander(f"Suppressed findings ({len(defect_report['suppressed'])}) — not raised to avoid misleading defects"):
                st.dataframe(pd.DataFrame(defect_report["suppressed"]), use_container_width=True, hide_index=True)

    for anomaly in defect_report["anomalies"]:
        st.error(f"**Attention required.** {anomaly}", icon="🚨")

    if defects:
        st.warning(f"{len(defects)} defect draft(s) generated — status: Draft - Pending Review.")
        for defect in defects:
            with st.expander(f"{defect['defect_id']} — {defect['defect_title']}"):
                st.markdown(f"**Severity:** {defect['severity']}")
                st.markdown(f"**Record identifier:** {defect['record_identifier']}")
                st.markdown(f"**Issue category:** {defect['issue_category']}")
                st.markdown(f"**Affected field:** {defect['affected_field']}")
                if defect.get("transformation"):
                    st.markdown(f"**STTM transformation:** `{defect['transformation']}`")
                    values = st.columns(3)
                    values[0].markdown("**Source value**")
                    values[0].code(json.dumps(defect.get("source_value"), default=str))
                    values[1].markdown("**Expected value (after transformation)**")
                    values[1].code(json.dumps(defect["expected_value"], default=str))
                    values[2].markdown("**Actual value (target)**")
                    values[2].code(json.dumps(defect["actual_value"], default=str))
                else:
                    values = st.columns(2)
                    values[0].markdown("**Expected value (source)**")
                    values[0].code(json.dumps(defect["expected_value"], default=str))
                    values[1].markdown("**Actual value (target)**")
                    values[1].code(json.dumps(defect["actual_value"], default=str))
                st.markdown("**Defect description**")
                st.write(defect["defect_description"])
                st.markdown("**Preconditions**")
                st.markdown("\n".join(f"- {item}" for item in defect["preconditions"]))
                st.markdown("**Steps to reproduce**")
                st.markdown("\n".join(f"{i}. {step}" for i, step in enumerate(defect["steps_to_reproduce"], start=1)))
                st.markdown(f"**Expected result:** {defect['expected_result']}")
                st.markdown(f"**Actual result:** {defect['actual_result']}")
                st.markdown(f"**Probable cause:** {defect['probable_cause']}")
                st.markdown(f"**Business impact:** {defect['business_impact']}")
                st.caption(f"Status: {defect['status']}")
        downloads = st.columns(2)
        downloads[0].download_button("Download defect drafts (JSON)", data=json.dumps(defect_report, indent=2, default=str), file_name="defect_drafts.json", mime="application/json", use_container_width=True)
        downloads[1].download_button("Download defect drafts (CSV)", data=defects_to_csv(defects).encode("utf-8"), file_name="defect_drafts.csv", mime="text/csv", use_container_width=True)
    elif defect_report["validation_status"] == "PASS":
        st.success("No defects identified")
    else:
        st.info("No reliable defect drafts could be generated from this reconciliation result.")


def show_last_run():
    """Results-only view of the latest `python -m src.main` run, read from the reports it wrote.

    File paths come only from reports/last_run.json, never from the URL.
    """
    try:
        run = load_json(ROOT / "reports" / "last_run.json")
        if not isinstance(run, dict) or not all(run.get(k) for k in ("validation_report", "defects_report")):
            raise ValueError("reports/last_run.json does not name the run's reports; run the validation again")
        result = load_json(Path(run["validation_report"]))
        defect_report = load_json(Path(run["defects_report"]))
    except (OSError, ValueError, UnicodeError) as exc:
        st.error(f"Unable to open the last command-line run: {exc}")
        return
    st.caption(f"Run {run.get('timestamp', 'at unknown time')} • "
               f"{db_label('Source', run.get('source_name'))} → {db_label('Target', run.get('target_name'))}")
    try:
        render_validation(result, defect_report)
        st.divider()
        render_defects(defect_report)
    except (KeyError, TypeError, AttributeError) as exc:
        st.error(f"The saved reports are not in the expected format: {exc}")


st.title("Source ↔ Target Validation")

if st.query_params.get("run") == "last":
    show_last_run()
    st.stop()

# ---- Interactive sample mode -------------------------------------------------------------------
st.caption("Sample mode • Local JSON files • No database connections")

with st.sidebar:
    st.header("Validation settings")
    st.info("Sample mode only. Uploaded data is processed in this app session.")
    source_db_name = st.text_input("Source DB name", key="source_db_name", placeholder="e.g. FinanceERP",
                                   help="Used in defect drafts. Leave blank for the generic 'Source DB'.")
    target_db_name = st.text_input("Target DB name", key="target_db_name", placeholder="e.g. receivables-db",
                                   help="Used in defect drafts. Leave blank for the generic 'Target DB'.")
    source_upload = st.file_uploader("Source JSON", type=["json"], key="source")
    target_upload = st.file_uploader("Target JSON", type=["json"], key="target")
    sttm_upload = st.file_uploader("STTM mapping file", type=["json", "csv", "xlsx"], key="sttm")
    mapping_label = f"uploaded file {sttm_upload.name}" if sttm_upload else "config/mapping.json"
    source_label = f"uploaded file {source_upload.name}" if source_upload else "data/sql_sample.json"
    target_label = f"uploaded file {target_upload.name}" if target_upload else "data/cosmos_sample.json"
    st.caption(f"Mapping: {mapping_label}")
    st.caption("CSV/Excel columns: source, target, type (string/decimal/date/boolean), key (Y on the business key row), "
               "and optionally transformation (e.g. `trim | upper`, `multiply(100)`, `map(A=Active; I=Inactive)`; "
               "blank or Direct = copied as is). "
               "A row with only a Target name (e.g. ReceivableHeader), or with both names and a blank type "
               "(e.g. ProductDetails / item), groups the rows below it under that object.")
    st.download_button("Download STTM CSV template", data=CSV_TEMPLATE.encode("utf-8"), file_name="sttm_template.csv", mime="text/csv")

try:
    if sttm_upload is not None:
        mapping = parse_sttm(sttm_upload.getvalue(), sttm_upload.name)
    else:
        mapping = validate_mapping(load_json(ROOT / "config" / "mapping.json"))
    source_records = parse_records(source_upload, ROOT / "data" / "sql_sample.json")
    target_records = parse_records(target_upload, ROOT / "data" / "cosmos_sample.json")
except (OSError, json.JSONDecodeError, ValueError, UnicodeError) as exc:
    st.error(f"Unable to load sample data or mapping: {exc}")
    st.stop()

if sttm_upload is not None:
    with st.sidebar:
        st.caption("Review the uploaded mapping under **View field mapping** before saving. The current file is backed up first.")
        if st.button("Save as default mapping (config/mapping.json)", use_container_width=True):
            try:
                backup = save_mapping(mapping, ROOT / "config" / "mapping.json")
                st.success("Saved to config/mapping.json" + (f" — previous version backed up to config/{backup.name}" if backup else ""))
            except OSError as exc:
                st.error(f"Unable to save mapping: {exc}")

st.subheader("Sample data")
left, right = st.columns(2)
with left:
    st.write(f"**Source** — {len(source_records)} records")
    st.dataframe(pd.json_normalize(source_records), use_container_width=True, hide_index=True)
with right:
    st.write(f"**Target** — {len(target_records)} records")
    st.dataframe(pd.json_normalize(target_records), use_container_width=True, hide_index=True)

with st.expander("View field mapping"):
    st.dataframe(pd.DataFrame(mapping["fields"]), use_container_width=True, hide_index=True)
    st.caption(f"Business key: {mapping['key'].get('source')} → {mapping['key'].get('target')}")

if st.button("Run validation", type="primary", use_container_width=True):
    try:
        st.session_state["validation_result"] = reconcile(source_records, target_records, mapping)
        st.session_state["validation_input_signature"] = json.dumps([source_records, target_records, mapping], sort_keys=True, default=str)
    except (KeyError, TypeError, ValueError) as exc:
        st.error(f"Validation failed: {exc}")

signature = json.dumps([source_records, target_records, mapping], sort_keys=True, default=str)
result = st.session_state.get("validation_result")
if result is not None and st.session_state.get("validation_input_signature") != signature:
    st.warning("Sample inputs changed. Click Run validation to refresh the report.")
elif result is not None:
    defect_report = build_defect_report(result, mapping, source_records, target_records, {
        "source_dataset": source_label,
        "target_dataset": target_label,
        "mapping_file": mapping_label,
        "source_name": source_db_name,
        "target_name": target_db_name,
    })
    render_validation(result, defect_report)
    st.divider()
    render_defects(defect_report)
else:
    st.info("Click **Run validation** to compare the sample datasets.")
