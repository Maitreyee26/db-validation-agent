---
name: review-defects
description: Check whether the defect drafts produced by this project's Python generator (src/defect_generator.py) are proper. Runs the draft rules check on a saved defect_drafts.json (from reports/ or downloaded from the Streamlit UI), or on drafts generated from built-in edge-case scenarios, and reports every rule violation. Use whenever the user asks to review, check, audit or sanity-test the defect drafts or defect descriptions, even if they don't name this skill. It checks the drafts only; it does not run a code review.
argument-hint: "[path to defect_drafts.json] [Source DB name] [Target DB name]"
---

# Check the defect drafts

Testers file these drafts as defects, so a wrong draft turns directly into a misleading defect: a wrong value, a guessed cause stated as fact, a record said to be missing when it isn't. This skill checks the drafts the Python generator produced against the db-validation-agent reporting rules, and nothing else. Do not run `/code-review` or the unit tests as part of it.

Arguments from the user: $ARGUMENTS

They may include a path to a saved `defect_drafts.json` and Source/Target DB names. All are optional.

Work from the project root, the folder that holds `app.py`, and use the project interpreter `.venv/Scripts/python.exe`. The system Python may not have the project's packages.

## 1. Run the draft rules check

If the user gave a saved report, check that report. A saved report is `reports/defect_drafts.json` after a command-line or `/validate-db` run, or the file from **Download defect drafts (JSON)** in the UI. Pass the DB names exactly as the user wrote them:

```
.venv/Scripts/python.exe .claude/skills/review-defects/scripts/check_drafts.py --report "<path>" --source-name "<name>" --target-name "<name>"
```

Leave out a name flag when the user didn't give that name. The check then only requires that each side is named consistently.

A saved report is also compared with the records it was generated from: each field's expected and actual value, whether a record said to be missing or extra really is, and duplicate counts. The check finds the source, target and mapping files in `reports/last_run.json` when that run wrote the report, and prints which files it compared with. For any other report, such as one downloaded from the UI, add the files the user validated: `--source-file "<source>" --target-file "<target>" --mapping "<STTM file>"`. If you don't know them, ask the user, or report that values were not compared. If the output has a `note:` that a file changed after the run, mention it alongside any value violation.

With no saved report, generate drafts from the built-in scenarios and check them:

```
.venv/Scripts/python.exe .claude/skills/review-defects/scripts/check_drafts.py
```

The scenarios are the bundled sample data with `config/mapping.json`, plus edge cases: explicit null vs absent attributes, empty arrays, NaN/Infinity, extreme decimals, boolean fields (1 vs true, Y vs false, unparseable flags), formula-like strings and keys, duplicate and missing business keys, extra/missing records, and STTM transformations (map, arithmetic, concat of sibling fields, default on null vs absent, dates, and Source values a rule cannot process). Each is run with and without DB names.

Exit code 0 means every rule holds, 1 means violations (listed per draft with a rule id), and 2 means the check could not run. Each rule id maps to a rule in `.claude/agents/db-validation-agent.md`:

| Rule | What it enforces |
|---|---|
| R-STATUS | PASS only with no drafts; FAIL has drafts; INCOMPLETE gives reasons |
| R-INCOMPLETE | Duplicate or missing business keys make the result INCOMPLETE |
| R-DRAFT | Status is "Draft - Pending Review"; nothing is submitted |
| R-CATEGORY | Only the six known issue categories |
| R-HYPOTHESIS | Probable cause is stated as an unverified hypothesis |
| R-CRITICAL | Extra-in-target is Critical and listed in anomalies; nothing else is Critical |
| R-WORDING | Drafts say Source and Target, not SQL Server or Cosmos |
| R-DBNAMES | DB names appear exactly as given, generic wording when none was given, consistent throughout |
| R-PATHS | No file paths in steps to reproduce |
| R-IMPACT | Business impact for field defects only when backed by the result (e.g. a numeric variance) |
| R-VALUES | Expected/actual values, missing/extra records and duplicate counts match the records; identical values are never reported as a mismatch. For a field with an STTM transformation, the draft's rule matches the STTM, `source_value` matches the record and `expected_value` is that rule applied to it |
| R-NULL-ABSENT | An absent attribute and an explicit null stay distinct in text and value columns |
| R-CSV | No cell in the CSV export would run as a formula in Excel |
| R-UNIQUE | Unique defect ids, no duplicate drafts |
| R-FIELDS | Every required draft field is present |
| R-CRASH | Generating the report or the CSV export raised an error |

A violation is a finding to investigate, not automatically a generator bug. In report mode, a value from the user's own data can trip a rule; for example, a status containing the word "Cosmos" trips R-WORDING. Read the draft before calling it a bug.

## 2. Report

Keep the report as quiet as the check's output.

**No violations (exit code 0):** reply with the status line only: what was checked (the report path, or the built-in scenarios) · whether values were compared with the records · drafts checked · 0 violations, every rule holds. Add one more line only when the output says values were NOT compared with the records (say why), or has a `note:` that a file changed after the run. Don't add a Findings or Not checked section, and don't read the drafts file.

**Violations (exit code 1):** use this structure:

1. **Status line**, as above, with the number of violations.
2. **Findings**: one item per problem, most severe first; violations with the same cause are one item. For each, give the defect id, the rule id, what is wrong, the draft text or value that breaks the rule, and whether it comes from the generator or from the user's own data. When it comes from the generator, cite the line in `src/defect_generator.py` that produces that text as `file:line`. Order of severity: wrong or misleading draft content, or a crash; then unsafe output, such as CSV formulas; then ambiguous or mislabelled text. To read a draft, Grep the report for its `"defect_id"` with about 30 lines after it rather than reading the whole file.
3. **Not checked**: anything outside the check's reach. If the output says values were NOT compared with the records, say so and why. The check does not confirm that every failure in the data got a draft, only that the drafts present are correct. Live mode and the UI's validation-results CSV are never checked.

**Could not run (exit code 2):** report the error output and stop.

Don't change code during the check. Offer to fix the findings. After a fix to the generator, run the unit tests (`.venv/Scripts/python.exe -m unittest discover -s tests`) and rerun this check.

Before checking a fix in the browser, restart the Streamlit UI if it's running. A running UI keeps the old versions of `src/` modules in memory, so it shows the old drafts until restarted. Stop the process listening on port 8501, then start `.venv/Scripts/python.exe -m streamlit run app.py`.
