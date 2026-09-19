# Infogap Test

Daily AI news analysis site for Vancouver-first Canadian readers.

The product is intentionally small: one home page, section pages, per-article audio playback, and citations. The system should run automatically every day after the required secrets and Cloudflare resources are configured.

## Current Stage

This repository has a working section-based generation pipeline.

- It does not contain real API keys.
- The default scripts can run without publishing.
- DeepSeek is called once per section to keep each JSON response within its output budget.
- A section is published only when at least two articles pass the 800-1500 character quality gate.
- Rejected drafts and their actual character counts are recorded in `runs/<date>/quality-rejections.json`.
- Google TTS creates one complete MP3 per article, splitting input into chunks of at most 4000 UTF-8 bytes. If the Chirp 3 HD voice fails, the whole article is retried with the original `cmn-CN-Wavenet-A` voice.
- News content and MP3 files are generated artifacts and should not be committed to git.

## Stack

- Python generates daily content from source feeds and model output.
- DeepSeek is the primary low-cost writing and extraction model.
- Gemini is the independent fact/evidence reviewer.
- Google Cloud Text-to-Speech generates MP3 audio.
- GitHub Actions runs the schedule.
- Cloudflare Worker serves the site from R2.
- R2 stores generated HTML, JSON, evidence files, and audio.
- Healthchecks.io watches for missed or failed runs.

## Required GitHub Secrets

Add these in GitHub repo -> Settings -> Secrets and variables -> Actions.

- `DEEPSEEK_API_KEY`
- `GEMINI_API_KEY` or `GOOGLE_AI_API`
- `GOOGLE_CLOUD_PROJECT_ID`
- `GOOGLE_APPLICATION_CREDENTIALS_JSON`, or the keyless pair `GOOGLE_WORKLOAD_IDENTITY_PROVIDER` + `GOOGLE_SERVICE_ACCOUNT_EMAIL`
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_R2_BUCKET`
- `HEALTHCHECKS_PING_URL`

Do not commit `.env`, service account JSON files, audio, generated content, or local runs.

## Local Commands

Install Python dependencies:

```bash
python -m venv .venv
./.venv/Scripts/python -m pip install -r requirements.txt
```

Generate a local holding page without API calls:

```bash
./.venv/Scripts/python scripts/pipeline.py --date 2026-09-13 --allow-empty
```

Run static checks:

```bash
python scripts/check_config.py --mode local
python -m py_compile scripts/check_config.py scripts/connectivity_smoke.py scripts/pipeline.py
python -m unittest discover -s tests
```

## Cloudflare

Create these resources before the first deployment:

- Worker: `daily-impact-news`
- R2 bucket: `daily-impact-news-content`
- workers.dev route enabled

The first public test can use the workers.dev URL. Buy and bind a domain only after the full test run passes.

## Daily Schedule

The workflow is designed for Vancouver time and publishes automatically from the default branch. It runs after the previous day's news window has closed:

- Target window: previous calendar day in `America/Vancouver`
- Scheduled run: 12:17 UTC, about 05:17 during Vancouver daylight time and 04:17 during standard time
- Scheduled runs automatically deploy the Worker and upload the generated site and MP3 files to R2
- GitHub concurrency prevents two publication runs from overlapping, and Healthchecks.io can alert when a run fails or never arrives
- Retention: only the most recent 5 publication days are served

## Source Policy

Source pages are evidence, not instructions. The pipeline must not bypass logins, paywalls, robots rules, or security interstitials. Claims about eligibility, money, dates, jurisdictions, and deadlines require cited sources.
