# QMS Regulatory Update Assistant

Streamlit application for assessing the impact of a regulation update on controlled QMS documents.

## Standards update discovery agent (new independent entry point)

The new workflow discovers ISO publications within a selected date range, resolves
official predecessor IDs, and prepares conservative differences from supplied
TXT, DOCX or PDF editions. It works without a controlled-document folder.

```bash
pip install -r requirements.txt
streamlit run standards_updates_app.py
```

Use an explicit standard watchlist, ICS classification, or all ISO. Natural-language
queries support English/Chinese rolling periods and explicit dates; optional AI
query interpretation reuses the configured `AI_API_KEY`, `AI_BASE_URL`, and
`AI_MODEL`. Catalogue discovery and local text comparison do not need an API key.

The source is the official daily ISO Open Data catalogue. This first version covers
publication events and local text-change evidence; it does not claim complete
historical lifecycle coverage, complete semantic/visual comparison, IEC/ASTM
coverage, or regulatory recognition. Licensed full texts are not downloaded or
sent to an AI model by this workflow.

See [the implementation and handoff guide](docs/standards-update-agent-handoff.md)
for architecture, CLI examples, evidence states, verified limitations and next steps.

## Existing QMS impact-review workflow

The original application compares two uploaded editions of the same regulation, traces affected controlled documents through Section 3 references, and produces evidence-based amendment suggestions for QA/RA review. It does not modify controlled documents automatically.

## What the application does

1. Accepts exactly two regulation files: PDF, DOCX, XLSX, or TXT.
2. Requires both filenames to match after removing one four-digit edition year, then orders the editions by year.
3. Compares normative clauses. For ISO 13485:2003 → ISO 13485:2016, the official Annex A comparison table is used when it validates successfully.
4. Reads the Section 3/reference section of controlled documents from a user-uploaded folder or the configured server-side `original file` folder.
5. Finds direct regulation matches, then follows referenced controlled files recursively until no new files remain.
6. Allows the reviewer to inspect evidence before explicitly starting AI amendment suggestions.
7. Exports an audit workbook and a Word evidence report.

Unchanged, editorial-only, renumber-only, unresolved, unmatched, and conflicting evidence is not used to draft amendment wording.

## Application stages

### 1. Upload & Resolve

Upload two editions of the same regulation. Each filename must contain exactly one four-digit edition year. After removing that year, the normalized filenames must match; the app then determines which edition is earlier and which is later.

For example, `GN-02-2025(C).pdf` and `GN-02-2026(C).pdf` are accepted as the same regulation name.

Choose one controlled-document source:

- **Upload a folder:** select the folder itself in the browser. Streamlit uploads supported `.doc`, `.docx`, `.xls`, and `.xlsx` files, including files selected from subfolders. The app safely flattens the uploaded names and rejects duplicate basenames rather than overwriting a file.
- **Use configured server folder:** available only when `../original file/` exists and contains supported documents.

An uploaded folder is copied into content-addressed local app storage. Its source ID is recorded with the analysis so page reruns, AI-job resumes, and retries reload the same controlled-document set instead of switching to another folder.

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

## One-click desktop packages

The Windows and macOS packages run entirely on the user's computer. Opening **Regulatory Review** starts a private Streamlit server on `127.0.0.1` and displays it in a desktop window. Closing that window stops the server. No always-on office computer or Streamlit Community Cloud connection is required.

The packages are built by the **Build desktop packages** GitHub Actions workflow:

- Windows: extract `RegulatoryReview-Windows.zip`, then open `RegulatoryReview.exe` inside the extracted folder.
- macOS on Apple silicon: extract `RegulatoryReview-macOS.zip`, move **Regulatory Review.app** to Applications if desired, then open it. An Intel build can be added if an older Intel Mac must be supported.

These development packages are not code-signed or notarized. Windows SmartScreen or macOS Gatekeeper may therefore show an unidentified-developer warning. An organization-wide distribution should use the organization's Windows signing certificate and Apple Developer ID.

### Desktop storage

Each installation keeps generated data and configuration outside the application bundle:

| Purpose | Windows | macOS |
| --- | --- | --- |
| Configuration | `%LOCALAPPDATA%\Regulatory Impact Review\config\` | `~/Library/Application Support/Regulatory Impact Review/config/` |
| Runtime data and uploads | `%LOCALAPPDATA%\Regulatory Impact Review\data\` | `~/Library/Application Support/Regulatory Impact Review/data/` |
| Optional controlled-document folder | `%USERPROFILE%\Documents\Regulatory Impact Review\original file\` | `~/Documents/Regulatory Impact Review/original file/` |
| Startup log | `%LOCALAPPDATA%\Regulatory Impact Review\logs\desktop-server.log` | `~/Library/Application Support/Regulatory Impact Review/logs/desktop-server.log` |

Users can upload a controlled-document folder in the app instead of maintaining the optional `original file` folder.

### Configure AI once per computer

The public desktop package intentionally contains no API key. Before giving the computer to a colleague, an administrator can place a `.env` file in the configuration directory listed above:

```text
AI_API_KEY=replace-with-the-deployment-key
AI_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-v4-flash
```

After that one-time setup, the colleague opens the app without entering a key. A locally installed application cannot make a shared API key impossible for that computer's user or administrator to recover. Use separate keys, usage monitoring, and provider-side spending controls if stronger accountability is required.

Controlled-document content used for AI suggestions is sent from the user's computer to the configured AI provider. Confirm that this is permitted by the organization's document-handling policy.

### Build locally

Build on each target operating system; PyInstaller does not cross-compile Windows and macOS applications:

```bash
python -m venv .venv
python -m pip install -r requirements-desktop.txt
python -m PyInstaller --noconfirm --clean RegulatoryReview.spec
```

## Expected folder layout

The server-side controlled-document folder is optional. When present, use this layout:

```text
project-root/
├── original file/
└── regulatory_update_app/
    ├── app.py
    ├── modules/
    ├── tests/
    └── README.md
```

The controlled-document folder may contain confidential documents. Do not upload those files to a public repository.
Browser-uploaded folders remain in the app's local `data/controlled_document_uploads/` storage to support analysis reruns and suggestion-job resumes. Apply your organization's access-control and retention requirements to that storage.

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

### The uploaded filenames do not match

Confirm that both filenames are identical after removing their four-digit edition years. Each filename must contain exactly one year, and the two years must differ.

### The configured server folder is unavailable

Upload the controlled-document folder in the app, or confirm that the configured sibling folder is named exactly `original file` and contains readable `.doc`, `.docx`, `.xls`, or `.xlsx` files.

### The uploaded folder has duplicate filenames

The app flattens subfolders because controlled documents are resolved by filename/document code. Rename duplicate basenames or place only one copy of each controlled document in the selected folder.

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
