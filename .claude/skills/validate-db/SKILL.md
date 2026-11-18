---
name: validate-db
description: Validate a Source JSON extract against a Target (Cosmos) JSON extract using an STTM mapping (.xlsx, .csv or .json) and generate defect drafts with the project's deterministic Python engine. Use whenever the user asks to validate, reconcile or compare source and target records or payloads, or to raise defects from such a comparison, even if they don't name this skill.
argument-hint: "[source.json] [target.json] [STTM file] [Source DB name] [Target DB name] [no-ui]"
---

# Validate Source vs Target and draft defects

The comparison and the defect drafts come from the project's Python engine (`src/main.py`), never from reading the JSON yourself. Testers file these drafts as defects, so every count and value must come from code that actually ran. Your job is to pick the inputs, run the engine, and report what it produced.

Arguments from the user: $ARGUMENTS

Work from the project root (the folder that holds `app.py`) with the project interpreter `.venv/Scripts/python.exe`. The system Python may not have the project's packages.

Before reporting, read `.claude/agents/db-validation-agent.md` and follow its Database Safety Rules, Defect Generation Rules, Validation Issue Categories, Output Format and Overall Status Rules. That file is the single source of truth for how results are reported; this skill covers how to run a validation.

## 1. Identify the inputs

Take them from the arguments and the user's message:

| Input | When not given |
|---|---|
| Source JSON file | `data/sql_sample.json` |
| Target JSON file | `data/cosmos_sample.json` |
| STTM mapping (`.xlsx`, `.csv` or `.json`) | `config/mapping.json` |
| Source DB name | omitted; drafts say "Source DB" |
| Target (Cosmos) DB name | omitted; drafts say "Target DB" |
| `no-ui` or "chat only" | the UI opens |

- Paths often contain spaces (e.g. OneDrive folders), so keep them quoted.
- Use the DB names exactly as the user wrote them. Never derive a name from a file name.
- If the user gave no files at all, say you are using the bundled sample data before running.

## 2. Check the files exist

Check every path before running. If one is missing, look in the same folder for that name with a doubled extension, e.g. `source.json.json`: Windows hides known extensions, so Explorer shows that file as `source.json`.

- If exactly that doubled-extension file exists, use it and say so at the top of the report.
- Otherwise report the missing file and stop. Never substitute the sample files.

## 3. Run the engine

```
.venv/Scripts/python.exe -m src.main --mode sample --source-file "<source>" --target-file "<target>" --mapping "<STTM file>" --source-name "<Source DB name>" --target-name "<Target DB name>" --open-ui
```

- Leave out any option the user didn't give, so its default applies. Leave out `--open-ui` for `no-ui`.
- Exit code 0 means no mismatches. Exit code 1 means the engine ran and found mismatches, missing or extra records, or key issues; it is not an error. Any other exit code, or a traceback, is a VALIDATION_ENGINE_ERROR: report the output and set the overall status to ERROR.
- `--open-ui` starts the Streamlit UI if it isn't running and opens `http://localhost:8501/?run=last`, which shows this run's validation results and defect drafts.
- This skill runs sample mode only. If the user asks for live mode, stop: live mode needs their explicit approval and reviewed, bounded, read-only queries (see the agent's Database Safety Rules).

## 4. Report

Work from the engine's printed output: the counts, the status, `ATTENTION REQUIRED` anomalies, `INCOMPLETE` reasons, the suppressed count and one line per draft (`id | business key | category | field | expected | actual`, plus `source … rule …` for a transformed field). Don't read `reports/defect_drafts.json`, `reports/validation_report.json` or `reports/validation_report.csv` in full. The CSV is the row-by-row comparison (every mapped field of every record, Source next to Target, with the result and defect draft id); point the user to it for the full comparison and to the UI for what passed and what failed, and Grep it for one record's key only when the user asks about that record's fields. Open a single draft only when you need more than its line (to quote its description or steps, for a value marked `[truncated, N chars]`, or when the user asks about it): Grep `reports/defect_drafts.json` for `"defect_id": "DRAFT-00N"` with about 30 lines after it. Then report in the agent's Output Format, from Validation Summary to Overall Validation Status, with one row per draft. In particular:

- Put every entry in `anomalies` at the top of the Validation Summary.
- Use `validation_status`, `incomplete_reasons`, `defects` and `suppressed` from `defect_drafts.json` as they are. Never write drafts of your own. You may add business process, supporting evidence and suggested severity and priority, labelled as your suggestions.
- State probable causes as hypotheses, never as confirmed root causes.
- Say what the run did not cover, e.g. fields that are not in the mapping, or dates and amounts typed as string in the STTM.
- For a field with an STTM transformation, report the draft's `source_value`, `transformation` and `expected_value` together. The engine applied the rule; never apply or reinterpret it yourself.
- If the engine stops with `invalid transformation`, the STTM has a transformation cell that isn't one of the supported rules (listed in the error and in the README). Report the row and the text, and ask the user how to write it as a rule. Don't drop the row or rewrite the rule yourself.
- Never report PASS unless the engine ran and `validation_status` is PASS.

End by offering `/review-defects` on `reports/defect_drafts.json` with the same DB names.

## Updating the default mapping

Only when the user explicitly asks to make an STTM sheet the default mapping, run:

```
.venv/Scripts/python.exe -m src.sttm "<STTM file>" --out config/mapping.json
```

Show the printed mapping and the backup path it reports (`config/mapping.backup-<timestamp>.json`). Leave out `--out` to preview without writing.
