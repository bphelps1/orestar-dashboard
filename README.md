# Oregon Campaign Finance Dashboard

A self-maintaining, daily-refreshing dashboard of Oregon campaign finance data (cash contributions and expenditures, 2006–present), built on top of Oregon's [ORESTAR](https://secure.sos.state.or.us/orestar/) public records system.

**Live dashboard:** `https://bphelps1.github.io/orestar-dashboard/`

---

## Features

- **Daily automatic updates** via GitHub Actions (no manual work required)
- **Historical data** back to 2017 (load once via the Backfill workflow)
- **5 dashboard tabs:** Overview, Donors, Recipients, Timeline, Search
- **Fuzzy name deduplication** with a manual correction override file
- **Free hosting** on GitHub Pages — no server, no database, no ongoing cost

---

## One-Time Setup (15 minutes)

### 1. Create the repository

1. Go to [github.com/new](https://github.com/new)
2. Name it `orestar-dashboard`
3. Set visibility to **Public** (required for free GitHub Pages)
4. Click **Create repository**

### 2. Upload the project files

Either:
- Use the GitHub web UI to upload files, **or**
- Clone locally and push:
  ```bash
  git clone https://github.com/<your-username>/orestar-dashboard.git
  cd orestar-dashboard
  # copy these project files in, then:
  git add .
  git commit -m "Initial commit"
  git push
  ```

### 3. Enable GitHub Pages

1. Go to your repo → **Settings** → **Pages**
2. Under **Source**, select: `Deploy from a branch`
3. Branch: `main`, Folder: `/docs`
4. Click **Save**

Your dashboard will be live at: `https://<your-username>.github.io/orestar-dashboard/`
(It may take 1–2 minutes to first appear.)

### 4. Load historical data (one-time)

1. Go to your repo → **Actions** tab
2. Click **Historical Backfill (one-time)** in the left sidebar
3. Click **Run workflow** → set start year to `2017` → click **Run workflow**
4. The job takes ~30–60 minutes; when done, refresh your dashboard

### 5. Done!

Daily updates run automatically at 8am PST every day. No further action needed.

---

## How It Works

```
[GitHub Actions: daily cron at 8am PST]
        ↓
[scraper/fetch.py: downloads Excel exports from ORESTAR (last 14 days)]
        ↓
[scraper/process.py: cleans, deduplicates, aggregates → JSON files]
        ↓
[git commit → GitHub Pages → live dashboard]
```

### Data Pipeline Details

**fetch.py** uses Python `requests` (no browser needed) to:
1. Establish a session with ORESTAR
2. POST search queries (by 7-day windows, to stay under ORESTAR's 5,000-record limit)
3. Download Excel exports for Contributions (C) and Expenditures (E), Cash (CA) subtype

**process.py**:
1. Reads all Excel files, deduplicates by ORESTAR transaction ID
2. Normalizes contributor/committee names
3. Fuzzy-deduplicates names (>95% match = auto-merge; 85–95% = flagged for review)
4. Applies manual overrides from `scraper/entity_map.json`
5. Aggregates to JSON files in `data/aggregated/`
6. Updates `data/transactions.csv.gz`
7. Deletes raw Excel files (never committed to the repo)

---

## Correcting Name Variations

When the same donor appears under multiple spellings, you can fix it permanently:

1. Open `scraper/entity_map.json`
2. Add entries like:
   ```json
   {
     "Nike Inc.": "Nike Inc",
     "NIKE INC": "Nike Inc"
   }
   ```
3. Commit and push — corrections apply on the next daily run

Uncertain matches (85–95% fuzzy confidence) are written to `data/review_queue.json` for your inspection.

---

## Repository Structure

```
orestar-dashboard/
├── .github/workflows/
│   ├── daily-refresh.yml    # runs daily at 8am PST automatically
│   └── backfill.yml         # manual one-time historical pull
├── scraper/
│   ├── fetch.py             # downloads Excel exports from ORESTAR
│   ├── process.py           # cleans, deduplicates, aggregates
│   ├── entity_map.json      # manual name correction overrides
│   └── requirements.txt     # Python dependencies
├── data/
│   ├── transactions.csv.gz  # all processed transactions
│   └── aggregated/          # JSON files consumed by dashboard
├── docs/                    # GitHub Pages root
│   ├── index.html
│   ├── style.css
│   └── app.js
└── README.md
```

---

## Local Development

```bash
# Install dependencies
pip install -r scraper/requirements.txt

# Test connectivity (short date range)
python scraper/fetch.py --mode=test --days=7

# Process and generate JSON files
python scraper/process.py

# Open dashboard in browser
open docs/index.html
```

**Note:** `docs/index.html` loads data from `../data/aggregated/`. When opening directly from the filesystem (via `file://`), some browsers block the fetch calls. Use a local server instead:

```bash
# Python 3
python -m http.server 8000 --directory .
# Then open: http://localhost:8000/docs/
```

---

## Limitations & Notes

- **5,000-record limit per ORESTAR export.** The scraper uses 7-day windows to stay well under this. Weeks with unusually high activity (e.g., major election weeks) could theoretically exceed this; the scraper will log a warning if the returned row count is near the limit.
- **ORESTAR session-based.** No API key is required, but ORESTAR's URL structure or session handling could change. If the daily workflow starts failing, check the Actions logs.
- **GitHub Actions free tier.** Public repos have unlimited Actions minutes. The daily job takes ~5–10 minutes.
- **Data coverage.** Only cash (CA subtype) contributions and expenditures are included. In-kind contributions and loans are not currently pulled.

---

## Future Improvements

- Extend historical data back to 2007
- Candidate-level drill-down pages
- Full-text search across all records (requires backend: Datasette or Cloudflare D1)
- Email/Slack alerts for large contributions

---

## License

Data is public record from Oregon's Secretary of State. Code is MIT licensed.
