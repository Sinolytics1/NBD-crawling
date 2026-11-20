# NBD Transaction Scraper

Scrapes import or export transaction records from the NBD Transaction Search page (https://data.nbd.ltd), with the search form's filters (HS code, product keywords, company, dates) and any country on the result page.

## Quick start

**1. Install**

```bash
pip install playwright tqdm
playwright install chromium
```

**2. Log in**

Just run the script (step 3). If you are not logged in yet or the cookie has expired, the browser window stops at the NBD login page: enter your account, password and verification code there and click LOGIN. The script then continues and saves the cookie to `request_header.txt`, so the next run starts logged in. Never commit or share that file.

**3. Run**

```bash
python playwright_scrape_exporter.py --trade-type export --hs-code 48119000 --company "HENAN JIANGHE PAPER CO., LTD." --search-start 2025-11-19 --search-end 2026-11-19 --country CHINA
```

Each argument fills one field of the search form. Give at least one of `--hs-code`, `--keywords`, `--company`.

| Argument | Search form field | Default |
|---|---|---|
| `--trade-type import\|export` | Import or Export | `export` |
| `--hs-code` | HS Code | none |
| `--keywords` | Product Keywords (English) | none |
| `--keyword-langs` | languages ticked under "multilingual", comma separated: English, Russia, Spanish, Portugal, French, Ukrainian | none |
| `--company` | Company Name | none |
| `--search-start` / `--search-end` | Start Date / End Date (YYYY-MM-DD) | the last year up to today |
| `--country` | the country to scrape after Query, as shown on the page (`CHINA`, `UNITED KINGDOM`, …) | `CHINA` |

Example, import search with keywords:

```bash
python playwright_scrape_exporter.py --trade-type import --hs-code 481190 --keywords "thermal paper" --keyword-langs russian,ukrainian --country UKRAINE
```

## Output

The CSV name is built from the search arguments, e.g. `china_export_48119000_HENAN_JIANGHE_PAPER_CO_LTD_20251119-20261119.csv`. The same arguments always map to the same file, so if a run stops, run the same command again to resume; rows already saved are skipped. A different search never mixes into it. Use `--out` to choose another name.

One row per transaction: Date, Export country or region, Import country or region, HS Code, Product Description, Exporter (Importer for import searches), Quantity, Transport Mode, View (link to the detail page).

## Optional: fetch detail pages

The list only shows a truncated description and no value. Add `--detail TRUE` to the same command to also open each record's detail page and add **Description Of Goods, USD Amount(FOB), Price in USD(FOB), Weight**:

```bash
python playwright_scrape_exporter.py --trade-type export --hs-code 48119000 --company "HENAN JIANGHE PAPER CO., LTD." --search-start 2025-11-19 --search-end 2026-11-19 --country CHINA --detail TRUE
```

- If the CSV for this search does not exist yet, the script scrapes the list first and then fetches the details, so one run gives the enriched CSV. If it exists, only the details are fetched.
- Rows that already have details are skipped, and the CSV is saved after every row, so a stopped run resumes where it left off.
- This is slow because it opens one page per row.

| Argument | Meaning | Default |
|---|---|---|
| `--detail-start` / `--detail-end` | only fetch details for rows with Date in this range | the search range |
| `--detail-batch-rows` | also write every N rows to a batch CSV | 200 |
| `--detail-dir` | folder for the batch CSVs | `<CSV name>_details/` |
| `--detail-pause-min` | pause after 3 detail-page timeouts in a row | 30 |

## What has been scraped so far

HS 850710 (lead-acid batteries), export transactions. The data files are not included in this repository.

| File | Rows | Dates | Detail fields |
|---|---|---|---|
| `china_exporter_transaction.csv` | ~3,300 | 2025-11-30 – 2026-01-01 | Most rows |
| `vietnam_exporter_transaction_details.csv` | 27,914 | 2025-03-28 – 2026-02-05 | Few rows |
| `detailed data/` (China) | 2,800 | 2025-12-03 – 2025-12-31 | All rows |
| `detailed data vietnam/` | 1,600 | 2025-12-20 – 2025-12-31 | All rows |
