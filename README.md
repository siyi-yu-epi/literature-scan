# Literature Scan

Weekly literature scanner for selected marketing, management, and operations journals. The workflow finds articles that are not already in Notion, summarizes abstracts with Gemini 3.1 Pro on Vertex AI, generates English and Chinese summaries, scores relevance to Ruth's research focus, creates Notion data source entries, and sends a weekly email digest.

## Journals

- Journal of Marketing
- Journal of Marketing Research
- Marketing Science
- Management Science
- Manufacturing & Service Operations Management
- Production and Operations Management
- Quantitative Marketing and Economics

## Files

- `scripts/literature_scan.py` - CLI for creating the Notion database/data source and running scans.
- `config/journals.json` - journal ISSNs and optional RSS feeds.
- `.github/workflows/literature-scan.yml` - weekly GitHub Actions schedule.
- `.env.example` - local environment variable template.

## Setup

1. Install Python dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Create a Notion integration and share the parent Notion page with it.

3. Set the environment variables in `.env.example`.

4. Create the Notion database/data source:

   ```powershell
   python scripts/literature_scan.py create-database
   ```

   Copy the printed data source ID into `NOTION_DATA_SOURCE_ID`.

5. Test the scanner without creating Notion pages:

   ```powershell
   python scripts/literature_scan.py scan --dry-run --lookback-days 14
   ```

6. Run for real:

   ```powershell
   python scripts/literature_scan.py scan --lookback-days 14
   ```

7. Upgrade an existing Notion data source with the latest properties:

   ```powershell
   python scripts/literature_scan.py ensure-schema
   ```

8. Run for real and send the digest email:

   ```powershell
   python scripts/literature_scan.py scan --lookback-days 14 --send-email
   ```

## GitHub Actions

Add these repository secrets:

- `NOTION_TOKEN`
- `NOTION_DATA_SOURCE_ID`
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_CLOUD_LOCATION`
- `GOOGLE_APPLICATION_CREDENTIALS_JSON`
- `EMAIL_FROM`
- `GMAIL_CREDENTIALS_JSON`
- `GMAIL_TOKEN_JSON`

Alternatively, omit Gmail API secrets and set `SENDGRID_API_KEY`, or use SMTP:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`

The workflow is scheduled for every Monday at 4:17 AM America/New_York. It uses GitHub Actions' [native timezone support](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule) to handle daylight saving time with a single weekly trigger.

GitHub can delay scheduled runs, so the workflow always proceeds when triggered, even if the runner starts hours later. Do not add a check against the runner's current hour or weekday: the previous guard silently skipped delayed scans while reporting a successful job. Manual runs are also available through **Actions > Literature Scan > Run workflow**.

## Notion Properties

The `create-database` command creates a Notion database with an initial data source containing:

- `Title` - title
- `Authors` - text
- `Journal` - select
- `URL` - URL
- `DOI` - text
- `Published Date` - date
- `Abstract` - text
- `Abstract Summary` - text
- `Chinese Summary` - text
- `Keywords` - multi-select
- `Relevance Score` - number
- `Research Relevance` - multi-select with `LLM`, `AI`, `Healthcare`, `Policy Evaluation`, `Advertising`
- `Digest Included` - checkbox
- `Digest Reason` - text
- `Status` - select, default value `Unread`
- `Source Type` - select with `RSS`, `Publisher Page`, `Article Page`
- `Date Added` - date
- `Run ID` - text
- `Run Requested At` - date
- `Run Completed At` - date

## Weekly Digest

The email digest is sent to `yu1344@purdue.edu` by default. It includes:

- Total new papers added during the run
- Breakdown by journal
- English and Chinese trend summary
- Highlighted papers most relevant to `LLM`, `AI`, `Healthcare`, `Policy Evaluation`, and `Advertising`
- Title, authors, journal, relevance score, Chinese summary, URL, and full abstract for each highlighted paper

### Gmail API Setup

1. In Google Cloud Console, enable the Gmail API for the project.
2. Configure the OAuth consent screen.
3. Create an OAuth client ID with application type `Desktop app`.
4. Download the OAuth client JSON as `gmail-credentials.json` in this folder.
5. Run:

   ```powershell
   python scripts/literature_scan.py gmail-auth
   ```

6. Sign in to the Gmail account that should send the digest and approve the `gmail.send` permission.
7. The command writes `gmail-token.json`.
8. For GitHub Actions, add the full contents of `gmail-credentials.json` as `GMAIL_CREDENTIALS_JSON` and the full contents of `gmail-token.json` as `GMAIL_TOKEN_JSON`.
