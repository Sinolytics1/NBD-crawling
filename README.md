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

The cookie expires after a while. If the script lands on a login page, copy a fresh one. Never commit or share `request_header.txt`, because it gives access to your account.

**3. Run**

```bash
python playwright_scrape_exporter.py --country CHINA --start-date 2025-12-31
```

- `--country`: `CHINA` or `VIETNAM`
- `--start-date`: the script pages backwards from the newest records and starts saving once Date ≤ this date

The HS code and date range are set as constants at the top of the script (`SEARCH_HS_CODE`, `SEARCH_START_DATE`, `SEARCH_DATE_CLOSED`).

## Output

`<country>_exporter_transaction.csv` with one row per transaction:

Date, Export country or region, Import country or region, HS Code, Product Description, Exporter, Quantity, View (link to the detail page).

If the run stops, run the same command again to resume. Rows already saved are skipped.

## Optional: fetch detail pages

The list only shows a truncated description and no value. To open each record's detail page and add **Description Of Goods, USD Amount(FOB), Price in USD(FOB), Weight**, run the script again on the existing CSV:

```bash
python playwright_scrape_exporter.py --country CHINA --detail TRUE
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
