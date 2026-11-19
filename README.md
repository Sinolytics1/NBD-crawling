# NBD Export Transaction Scraper

Scrapes export transaction records from the NBD Transaction Search page (https://data.nbd.ltd) for a given HS code and exporting country (China or Vietnam).

## Quick start

**1. Install**

```bash
pip install playwright tqdm
playwright install chromium
```

**2. Set up `request_header.txt`**

Copy `request_header_sample.txt` to `request_header.txt`. Then log in to https://data.nbd.ltd with your own account, open the browser's developer tools (F12 → Network), reload the Transaction Search page and copy the `Cookie:` value of the page request into the file. Only the `Cookie:` line is required.

This step is optional. Without a cookie, or when the cookie has expired, the site shows its login page in the browser window the script opens. The script then waits up to 15 minutes (`LOGIN_WAIT_SEC`) for you to log in, and continues afterwards. It also writes the new cookies into the `Cookie:` line of `request_header.txt`, so the next run starts logged in.

To have the account and password filled in for you, create a `.env` file next to the script:

```
NBD_USERNAME=your_username_or_email
NBD_PASSWORD=your_password
```

The script picks the login type from the account (`Email` if it contains `@`, otherwise `Username`), fills both fields and leaves only the verification code and the LOGIN click to you. Without `.env`, choose Email/Username in the dropdown yourself: logging in with a username while the dropdown says Email fails with "bad credentials". Never commit `.env`.

Never commit or share `request_header.txt`, because it gives access to your account.

**3. Run**

```bash
python playwright_scrape_exporter.py --country CHINA --hs-code 48119000 \
  --company "HENAN JIANGHE PAPER CO., LTD." \
  --search-start 2025-11-19 --search-end 2026-11-19
```

- `--country`: `CHINA` or `VIETNAM` (the country button clicked after Query)
- `--hs-code`: HS code in the search form
- `--company`: Company Name in the search form; leave out to search all companies
- `--search-start` / `--search-end`: Start Date / End Date in the search form (YYYY-MM-DD)
- `--start-date` (optional): the script pages backwards from the newest records and starts saving once Date ≤ this date. Defaults to `--search-end`, so everything in the search range is saved.
- `--out` (optional): output CSV name. Relative paths are placed next to the script.

Defaults for the search fields are the constants at the top of the script (`SEARCH_HS_CODE`, `SEARCH_COMPANY_NAME`, `SEARCH_START_DATE`, `SEARCH_DATE_CLOSED`).

## Output

Without `--out`, the CSV name is built from the search: `<country>_<hs code>_<company>_<start>-<end>.csv`, e.g. `china_48119000_HENAN_JIANGHE_PAPER_CO_LTD_20251119-20261119.csv`. The same search always writes to the same file, so a rerun resumes it, and a different search never mixes into it. A `_meta.json` file is written next to it.

One row per transaction:

Date, Export country or region, Import country or region, HS Code, Product Description, Exporter, Quantity, View (link to the detail page).

If the run stops, run the same command again to resume. Rows already saved are skipped.

## Optional: fetch detail pages

The list only shows a truncated description and no value. To open each record's detail page and add **Description Of Goods, USD Amount(FOB), Price in USD(FOB), Weight**, run the script again on the existing CSV:

```bash
python playwright_scrape_exporter.py --country CHINA --detail TRUE   # plus the same search arguments as step 3
```

- Runs only when the CSV from step 3 already exists. It fills rows in `DETAIL_RANGE_START`–`DETAIL_RANGE_END`, newest first.
- This is slow because it opens one page per row. Results are also written every 200 rows to `detailed data/detail_batch_xxxx.csv`.
- That folder is shared by both countries, so rename it before switching country.

## What has been scraped so far

HS 850710 (lead-acid batteries), export transactions. The data files are not included in this repository.

| File | Rows | Dates | Detail fields |
|---|---|---|---|
| `china_exporter_transaction.csv` | ~3,300 | 2025-11-30 – 2026-01-01 | Most rows |
| `vietnam_exporter_transaction_details.csv` | 27,914 | 2025-03-28 – 2026-02-05 | Few rows |
| `detailed data/` (China) | 2,800 | 2025-12-03 – 2025-12-31 | All rows |
| `detailed data vietnam/` | 1,600 | 2025-12-20 – 2025-12-31 | All rows |
