# QMS Regulatory Update Assistant

Streamlit application for assessing the impact of a regulation update on controlled QMS documents.

The application compares two uploaded editions of the same regulation, traces affected controlled documents through Section 3 references, and produces evidence-based amendment suggestions for QA/RA review. It does not modify controlled documents automatically.

## What the application does

1. Accepts exactly two regulation files: PDF, DOCX, XLSX, or TXT.
2. Resolves the regulation family and edition order using document content, filenames, and the read-only `原始总表` alias registry.
3. Compares normative clauses. For ISO 13485:2003 → ISO 13485:2016, the official Annex A comparison table is used when it validates successfully.
4. Reads the Section 3/reference section of controlled documents in the configured `original file` folder.
5. Finds direct regulation matches, then follows referenced controlled files recursively until no new files remain.
6. Allows the reviewer to inspect evidence before explicitly starting AI amendment suggestions.
7. Exports an audit workbook and a Word evidence report.

Unchanged, editorial-only, renumber-only, unresolved, unmatched, and conflicting evidence is not used to draft amendment wording.

## Application stages

### 1. Upload & Resolve

Upload two editions of the same regulation. The app automatically determines which file is earlier and which is later.

The workbook and controlled-document folder are shown as read-only configured sources:

- `../AI-assisted regulatory updates.xlsx`
- `../original file/`

The workbook is used only as an alias registry. Its change-summary columns do not drive the comparison.

### 2. Change Evidence

Review the selected comparison method, accepted substantive changes, excluded rows, warnings, and any rows requiring human confirmation.

Each accepted change includes the old/latest clause text, clause identifiers, evidence method, official comparison statement where available, and a stable evidence ID.

### 3. Affected Files

Review direct Section 3 seeds, recursive dependency paths, depth, matched aliases, Section 3 evidence, unresolved document codes, and extraction/cache coverage.

Unresolved references remain visible for audit but are not passed to the AI suggestion worker.

### 4. Suggestions & Export

Click **Generate amendment suggestions** only after reviewing the deterministic evidence.

The persisted worker supports:

- Refresh;
- Pause;
- Resume;
- Retry failed items;
- QA/RA reviewer decisions and comments.

AI suggestions are generated only when actual controlled-document section text supports a regulation change. Section 3 presence alone is not sufficient evidence.

## Requirements

- Python 3.10 or newer;
- Streamlit;
- pandas;
- openpyxl;
- python-docx;
- python-dotenv;
- requests;
- LibreOffice or macOS `textutil` is recommended for converting legacy `.doc` controlled files.

## Installation

From the repository directory:

```bash
cd regulatory_update_app
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start the application:

```bash
streamlit run app.py
```

Then open the URL printed by Streamlit, normally:

```text
http://127.0.0.1:8501
```

## Expected folder layout

The app expects the alias workbook and controlled-document folder beside the application directory:

```text
project-root/
├── AI-assisted regulatory updates.xlsx
├── original file/
└── regulatory_update_app/
    ├── app.py
    ├── modules/
    ├── tests/
    └── README.md
```

The controlled-document folder may contain confidential documents. Do not upload those files to a public repository.

## API key configuration

The deterministic identity, comparison, dependency, and audit stages work without an API key. An API key is needed only for AI amendment suggestions.

For local use, create `.env` or `.env.txt` inside `regulatory_update_app/`:

```text
AI_API_KEY=your-own-key
AI_BASE_URL=https://your-approved-provider.example/v1/chat/completions
AI_MODEL=your-approved-model
```

Never commit `.env`, `.env.txt`, or any file containing a real key. Commit only a placeholder file such as `.env.example`:

```text
AI_API_KEY=replace-with-your-own-key
AI_BASE_URL=https://your-approved-provider.example/v1/chat/completions
AI_MODEL=your-approved-model
```

For Streamlit Community Cloud, place the values in the app's **Settings → Secrets** panel. For GitHub Actions, use repository or environment secrets and inject them only at runtime.

If a key was ever committed, revoke it immediately, issue a replacement, and remove the secret from Git history.

## Outputs and local data

The app stores local runtime data under `data/`, including:

- uploaded regulation copies;
- converted controlled files;
- extraction/reference caches;
- persisted suggestion jobs;
- audit files and reports.

These files may contain copyrighted standards or confidential QMS content. Keep them private and exclude them from Git unless your organization explicitly permits sharing them.

Typical exports are:

- `regulatory-impact-audit-<analysis-id>.xlsx`;
- `regulatory-impact-report-<analysis-id>.docx`.

All generated amendment text is draft-only. QA/RA approval and the organization's document-control process remain mandatory.

## Verification

Run the unit suite from `regulatory_update_app/`:

```bash
.venv/bin/python -m unittest discover tests
.venv/bin/python -m py_compile app.py modules/*.py
```

The real-sample smoke test uses two local regulation PDFs and requires access to the supplied files. Do not place licensed standards in a public repository.

## Troubleshooting

### The app cannot find the workbook

Confirm that `AI-assisted regulatory updates.xlsx` is beside `regulatory_update_app/`, not inside an unrelated folder.

### The app cannot find controlled documents

Confirm that the configured sibling folder is named exactly `original file` and contains readable source files.

### Legacy `.doc` files fail to convert

Install LibreOffice or ensure macOS `textutil` is available. Failed conversions are shown in the Affected Files audit and do not silently become AI suggestions.

### AI suggestions are disabled

Check that `AI_API_KEY` is available to the running process. The deterministic stages and audit exports remain usable without it.

### The app is slow on the first run

The first run converts and extracts controlled documents. Later runs reuse the document cache when the source hash and extractor version match.

## Safety and governance

- The app never edits controlled source files automatically.
- AI output is not regulatory approval.
- Suggestions must be checked against the full regulation, applicable laws, contracts, and internal procedures.
- Existing stricter requirements must not be weakened by a proposed amendment.
- Final changes require QA/RA review, approval, controlled release, and training where applicable.
