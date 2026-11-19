from __future__ import annotations

import argparse
import csv
import json
import time
from contextlib import suppress
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


REQUEST_HEADER_FILE = Path(__file__).with_name("request_header.txt")
TARGET_URL = "https://data.nbd.ltd/en/transaction/search?fl=1"
TARGET_COUNTRY = "CHINA"  # Optional: "CHINA" or "VIETNAM"
COUNTRY_CHOICES = {"CHINA", "VIETNAM"}

if TARGET_COUNTRY not in COUNTRY_CHOICES:
    raise ValueError(f"TARGET_COUNTRY must be one of {sorted(COUNTRY_CHOICES)}, got {TARGET_COUNTRY!r}")

_country_slug = TARGET_COUNTRY.strip().lower()
OUT_CSV = Path(__file__).with_name(f"{_country_slug}_exporter_transaction.csv")
OUT_META = Path(__file__).with_name(f"{_country_slug}_exporter_transaction_meta.json")
OUT_SCREENSHOT = Path(__file__).with_name(f"{_country_slug}_exporter_transaction.png")
OUT_DEBUG_HTML = Path(__file__).with_name(f"{_country_slug}_exporter_transaction_debug.html")
OUT_DEBUG_SCREENSHOT = Path(__file__).with_name(f"{_country_slug}_exporter_transaction_debug.png")
DETAIL_EXTRA_COLUMNS = [
    "Description Of Goods",
    "USD Amount(FOB)",
    "Price in USD(FOB)",
    "Weight",
]

# Table scrape writes list columns only. Detail columns are filled by enrich_csv_missing_details()
# and enrich_csv_details_date_range() when CSV exists (main()).
FETCH_DETAIL_PAGES = False

# Search form (transaction search page).
SEARCH_HS_CODE = "850710"
SEARCH_START_DATE = "2025-01-01"
SEARCH_DATE_CLOSED = "2025-12-31"
ROWS_PER_PAGE_ASSUMED = 10  # used with existing row count to skip pages when resuming
# Non-detail scrape starts writing once Date <= this anchor date (while clicking Next).
# Set to None to write from page 1.
NON_DETAIL_START_FROM_DATE = date(2025, 12, 31)

# CSV detail pass: find the first row with this Date, then process downward to end of file (file order).
CSV_ANCHOR_DATE = date(2025, 12, 30)

# After this many consecutive detail-page timeouts, pause (seconds).
DETAIL_TIMEOUT_BACKOFF_SEC = 1800

# When OUT_CSV exists and is non-empty, main() runs detail enrich only (table scrape is skipped).
# To run table scrape again, rename/remove OUT_CSV or empty the file first.
DETAIL_DATA_DIR = Path(__file__).with_name("detailed data")
DETAIL_BATCH_ROWS = 200
DETAIL_RANGE_START = date(2025, 1, 1)
DETAIL_RANGE_END = date(2025, 12, 31)


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_bool_arg(value: str) -> bool:
    v = (value or "").strip().lower()
    if v in {"true", "1", "yes", "y"}:
        return True
    if v in {"false", "0", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value!r}; use TRUE/FALSE")


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape NBD exporter transaction table (optionally detail enrich)."
    )
    parser.add_argument(
        "--country",
        default=TARGET_COUNTRY,
        type=str.upper,
        choices=sorted(COUNTRY_CHOICES),
        help="Country button to click (CHINA or VIETNAM).",
    )
    parser.add_argument(
        "--start-date",
        default=SEARCH_DATE_CLOSED,
        help="Start collecting non-detail rows when Date <= this value (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--detail",
        default=False,
        type=parse_bool_arg,
        help="TRUE: run detail enrich mode when CSV exists. FALSE: non-detail table scrape mode.",
    )
    return parser.parse_args()


def apply_runtime_config(args: argparse.Namespace) -> None:
    global TARGET_COUNTRY
    global OUT_CSV, OUT_META, OUT_SCREENSHOT, OUT_DEBUG_HTML, OUT_DEBUG_SCREENSHOT
    global NON_DETAIL_START_FROM_DATE, FETCH_DETAIL_PAGES

    TARGET_COUNTRY = args.country
    slug = TARGET_COUNTRY.strip().lower()
    OUT_CSV = Path(__file__).with_name(f"{slug}_exporter_transaction.csv")
    OUT_META = Path(__file__).with_name(f"{slug}_exporter_transaction_meta.json")
    OUT_SCREENSHOT = Path(__file__).with_name(f"{slug}_exporter_transaction.png")
    OUT_DEBUG_HTML = Path(__file__).with_name(f"{slug}_exporter_transaction_debug.html")
    OUT_DEBUG_SCREENSHOT = Path(__file__).with_name(f"{slug}_exporter_transaction_debug.png")

    NON_DETAIL_START_FROM_DATE = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    FETCH_DETAIL_PAGES = bool(args.detail)


def save_page_debug_snapshot(page: Page) -> None:
    """Persist HTML + full-page screenshot from the current session (same cookies / DOM as failure)."""
    try:
        OUT_DEBUG_HTML.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(OUT_DEBUG_SCREENSHOT), full_page=True)
        log(f"Debug HTML saved (this session): {OUT_DEBUG_HTML}")
        log(f"Debug screenshot saved (this session): {OUT_DEBUG_SCREENSHOT}")
    except Exception as dbg_exc:
        log(f"[DEBUG SAVE FAILED] {type(dbg_exc).__name__}: {dbg_exc}")


def get_cookie_header(file_path: Path) -> str:
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.lower().startswith("cookie:"):
            return line.split(":", 1)[1].strip()
    raise ValueError("Cookie header not found in request_header.txt")


def parse_cookie_header(cookie_header: str, target_url: str) -> list[dict]:
    host = urlparse(target_url).hostname
    if not host:
        raise ValueError("Invalid target URL")

    cookies: list[dict] = []
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": host,
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return cookies


def set_input_value(page: Page, label_text: str, value: str) -> None:
    log(f"         → set_input_value: wait visible for label {label_text!r} (≤30s)…")
    input_locator = page.locator(
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::input[1]"
    ).first
    input_locator.wait_for(state="visible", timeout=30000)
    input_locator.click()
    input_locator.fill(value)
    page.keyboard.press("Tab")
    log(f"         → set_input_value: done {label_text!r}")


def set_date_input_by_placeholder(page: Page, placeholder: str, value: str) -> None:
    log(f"         → date picker: wait visible placeholder={placeholder!r} (≤30s)…")
    input_locator = page.locator(
        f"input.form-control.date-picker[placeholder='{placeholder}']"
    ).first
    input_locator.wait_for(state="visible", timeout=30000)
    input_locator.evaluate(
        """(el, v) => {
            el.removeAttribute('readonly');
            el.value = v;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        }""",
        value,
    )
    log(f"         → date picker: set {placeholder!r} = {value!r}")


def choose_import_export_export(page: Page) -> None:
    log("         → waiting for Import/Export control (≤30s)…")
    select_input = page.locator(
        "xpath=//label[contains(normalize-space(), 'Import or Export')]/following::input[contains(@class, 'el-input__inner')][1]"
    ).first
    select_input.wait_for(state="visible", timeout=30000)
    select_input.click()
    log("         → waiting for Export option in dropdown…")
    option = page.locator(
        "xpath=//div[contains(@class, 'el-select-dropdown')]//li[normalize-space()='Export']"
    ).first
    option.wait_for(state="visible", timeout=30000)
    option.click()
    log("         → Export selected.")


def wait_table_loaded(page: Page, *, step: str | None = None) -> None:
    # Wait for either data rows or empty-state table body to finish loading.
    if step:
        log(f"         → wait_table_loaded start: {step}")
    page.wait_for_timeout(1200)
    loading = page.locator("div.tab-pane.active div.loading").first
    try:
        loading.wait_for(state="hidden", timeout=10000)
    except PlaywrightTimeoutError:
        if step:
            log(f"         → wait_table_loaded: loading overlay still visible after 10s ({step}), continuing")
    page.wait_for_timeout(500)
    if step:
        log(f"         → wait_table_loaded done: {step}")


def read_active_table(page: Page) -> tuple[list[str], list[list[str]]]:
    headers = [
        h.strip()
        for h in page.locator("div.tab-pane.active table thead tr th").all_inner_texts()
        if h.strip()
    ]
    rows: list[list[str]] = []
    row_locator = page.locator("div.tab-pane.active table tbody tr")
    row_count = row_locator.count()
    for i in range(row_count):
        row = row_locator.nth(i)
        cell_locator = row.locator("td")
        cell_count = cell_locator.count()
        cols: list[str] = []
        for j in range(cell_count):
            cell = cell_locator.nth(j)
            link = cell.locator("a[href]").first
            cell_text = cell.inner_text().strip()
            link_text = link.inner_text().strip() if link.count() > 0 else ""
            if cell_text == "View" and link_text == "View":
                href = (link.get_attribute("href") or "").strip()
                if href:
                    if href.startswith("http://") or href.startswith("https://"):
                        cols.append(href)
                    else:
                        cols.append(f"https://data.nbd.ltd{href}")
                    continue
            cols.append(cell_text)
        # Some tables have a leading empty utility column (before Date).
        if headers and len(cols) == len(headers) + 1 and cols[0] == "":
            cols = cols[1:]
        if cols:
            rows.append(cols)
    return headers, rows


def click_next_if_possible(page: Page) -> bool:
    next_btn = page.locator(
        "xpath=//div[contains(@class, 'tab-pane') and contains(@class, 'active')]//div[contains(@class,'pager-pages')]//*[self::a or self::button][contains(., 'Next')]"
    ).first
    if next_btn.count() == 0:
        return False

    cls = (next_btn.get_attribute("class") or "").lower()
    aria_disabled = (next_btn.get_attribute("aria-disabled") or "").lower()
    if "disabled" in cls or aria_disabled == "true":
        return False

    # Click and confirm pager changes.
    before_marker = page.locator(
        "xpath=//div[contains(@class, 'tab-pane') and contains(@class, 'active')]//div[contains(@class,'pager-pages')]"
    ).first.inner_text(timeout=5000)
    next_btn.click()
    page.wait_for_timeout(1200)
    wait_table_loaded(page)
    after_marker = page.locator(
        "xpath=//div[contains(@class, 'tab-pane') and contains(@class, 'active')]//div[contains(@class,'pager-pages')]"
    ).first.inner_text(timeout=5000)
    return before_marker != after_marker


# --- Search page → table scrape (main path; no detail pages) ---


def click_query_button(page: Page) -> None:
    log("         → waiting for Query button (≤30s)…")
    # Scope the button to the search-form area to avoid matching unrelated Query buttons.
    btn = page.locator(
        "xpath=(//div[contains(@class,'search') or contains(@class,'filter') or @id='search-form']"
        "//button[contains(normalize-space(), 'Query')])[1]"
    ).first
    if btn.count() == 0:
        # Fallback for layout variants.
        btn = page.locator("xpath=//button[contains(normalize-space(), 'Query')]").first
    btn.wait_for(state="visible", timeout=30000)
    btn.click()
    log("         → Query clicked.")


def click_country_button(page: Page, country: str) -> None:
    # Match pane like <div id="continent_1" class="tab-pane show active">…<button>VIETNAM (…)</button>
    # Do not require div.tab-content — that ancestor is often absent, which made the old chain never match.
    target_country = (country or "").strip().upper()
    if not target_country:
        raise ValueError("country must be non-empty")
    log(
        f"         → waiting for {target_country} button "
        f"(#continent_1.tab-pane button, has_text {target_country}, ≤120s)…"
    )
    btn = (
        page.locator("#continent_1.tab-pane button")
        .filter(has_text=target_country)
        .first
    )
    if btn.count() == 0:
        # Fallback for pages where active class/DOM nesting differs.
        btn = page.locator("#continent_1 button").filter(has_text=target_country).first
    btn.wait_for(state="visible", timeout=120000)
    log(f"         → {target_country} button visible, clicking…")
    btn.click()
    log(f"         → {target_country} clicked.")


def click_vietnam_country_button(page: Page) -> None:
    # Keep backward-compatible helper.
    click_country_button(page, "VIETNAM")


def click_exporter_transaction_details_tab(page: Page) -> None:
    log("         → waiting for 'Exporter Transaction Details' tab (≤30s)…")
    tab = page.locator(
        "xpath=(//*[contains(@class,'el-tabs__item') or contains(@class,'nav-link')]"
        "[contains(., 'Exporter Transaction Details')])[1]"
    ).first
    tab.wait_for(state="visible", timeout=30000)
    log("         → tab visible, clicking…")
    tab.click()
    log("         → Exporter Transaction Details tab clicked.")


def apply_search_filters(page: Page) -> None:
    log("      [3a/7] Set Import/Export to Export")
    choose_import_export_export(page)
    log("      [3b/7] Fill HS Code")
    set_input_value(page, "HS Code", SEARCH_HS_CODE)
    log("      [3c/7] Fill Start Date")
    set_date_input_by_placeholder(page, "Start Date", SEARCH_START_DATE)
    log("      [3d/7] Fill Date Closed")
    set_date_input_by_placeholder(page, "Date Closed", SEARCH_DATE_CLOSED)
    log("      [3e/7] Submit Query")
    click_query_button(page)
    page.wait_for_timeout(2000)
    wait_table_loaded(page, step="after Query")
    # Strong check: country panel should become interactable after Query.
    country_probe = page.locator("#continent_1 button").first
    country_probe.wait_for(state="visible", timeout=60000)
    log("         → Query effect confirmed: country buttons are visible.")


def load_csv_table_resume_state() -> tuple[set[tuple[str, ...]], list[str], list[list[str]]]:
    """Load existing table CSV for dedup + resume; strips trailing detail columns if present."""
    if not OUT_CSV.exists():
        return set(), [], []
    text = OUT_CSV.read_text(encoding="utf-8-sig")
    if not text.strip():
        return set(), [], []

    with OUT_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return set(), [], []

    header = [h.strip() for h in rows[0]]
    data_rows = [list(r) for r in rows[1:] if any((c or "").strip() for c in r)]

    if (
        len(header) >= len(DETAIL_EXTRA_COLUMNS)
        and header[-len(DETAIL_EXTRA_COLUMNS) :] == DETAIL_EXTRA_COLUMNS
    ):
        base_len = len(header) - len(DETAIL_EXTRA_COLUMNS)
        table_header = header[:base_len]
    else:
        base_len = len(header)
        table_header = list(header)

    seen: set[tuple[str, ...]] = set()
    norm_rows: list[list[str]] = []
    for r in data_rows:
        padded = r + [""] * max(0, len(header) - len(r))
        row_base = list(padded[:base_len])
        while len(row_base) < base_len:
            row_base.append("")
        base_tuple = tuple(row_base)
        if any(c.strip() for c in base_tuple):
            seen.add(base_tuple)
        norm_rows.append(row_base)

    return seen, table_header, norm_rows


# --- Detail page helpers (used by enrich_csv_missing_details / optional detail mode) ---


def get_following_td_text(page: Page, label: str) -> str:
    locator = page.locator(
        f"xpath=//td[contains(normalize-space(), '{label}')]/following-sibling::td[1]"
    ).first
    if locator.count() == 0:
        return ""
    try:
        return locator.inner_text(timeout=3000).strip()
    except PlaywrightTimeoutError:
        return ""


def get_usd_amount_and_price(detail_page: Page) -> tuple[str, str]:
    amount_labels = ["USD Amount(FOB)", "Amount (USD)"]
    price_labels = ["Price in USD(FOB)", "Unit Price (USD)"]

    amount_value = ""
    price_value = ""

    for label in amount_labels:
        amount_value = get_following_td_text(detail_page, label)
        if amount_value:
            break

    for label in price_labels:
        price_value = get_following_td_text(detail_page, label)
        if price_value:
            break

    if not amount_value:
        locator = detail_page.locator(
            "xpath=(//td[contains(translate(normalize-space(), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'USD') and contains(translate(normalize-space(), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'AMOUNT')]/following-sibling::td[1])[1]"
        ).first
        if locator.count() > 0:
            amount_value = locator.inner_text().strip()

    if not price_value:
        locator = detail_page.locator(
            "xpath=(//td[contains(translate(normalize-space(), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'USD') and contains(translate(normalize-space(), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'PRICE')]/following-sibling::td[1])[1]"
        ).first
        if locator.count() > 0:
            price_value = locator.inner_text().strip()

    return amount_value, price_value


def get_weight_value(detail_page: Page) -> str:
    net_weight = get_following_td_text(detail_page, "Net Weight")
    if net_weight:
        return net_weight

    locator = detail_page.locator(
        "xpath=(//td[contains(translate(normalize-space(), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'WEIGHT')]/following-sibling::td[1])[1]"
    ).first
    if locator.count() > 0:
        return locator.inner_text().strip()
    return ""


def get_view_url_from_row(row: list[str]) -> str:
    for value in row:
        if value.startswith("http://") or value.startswith("https://"):
            return value
    return ""


def fetch_detail_fields(context, detail_url: str, row_id: str) -> tuple[dict[str, str], bool]:
    """Returns (fields, timed_out). Used by enrich_csv_missing_details (opens each View URL)."""
    default = {k: "" for k in DETAIL_EXTRA_COLUMNS}
    if not detail_url:
        return default, False

    detail_page = context.new_page()
    try:
        detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        detail_page.wait_for_timeout(1000)
        usd_amount, usd_price = get_usd_amount_and_price(detail_page)
        fields = {
            "Description Of Goods": get_following_td_text(detail_page, "Description Of Goods"),
            "USD Amount(FOB)": usd_amount,
            "Price in USD(FOB)": usd_price,
            "Weight": get_weight_value(detail_page),
        }
        return fields, False
    except PlaywrightTimeoutError:
        log(f"      [detail {row_id}] TimeoutError")
        return default, True
    except Exception as exc:
        log(f"      [detail {row_id}] failed: {type(exc).__name__}")
        return default, False
    finally:
        with suppress(PlaywrightError):
            detail_page.close()


def load_existing_csv_state() -> tuple[set[tuple[str, ...]], int, list[str], list[str], bool]:
    """Returns (seen_base_keys, data_row_count, file_header, full_headers, file_exists_nonempty)."""
    if not OUT_CSV.exists():
        return set(), 0, [], [], False

    text = OUT_CSV.read_text(encoding="utf-8-sig")
    if not text.strip():
        return set(), 0, [], [], False

    with OUT_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        return set(), 0, [], [], False

    header = [h.strip() for h in rows[0]]
    data_rows = [r for r in rows[1:] if any(cell.strip() for cell in r)]

    # Base columns = table columns (everything before detail extras).
    if len(header) >= len(DETAIL_EXTRA_COLUMNS) and header[-len(DETAIL_EXTRA_COLUMNS) :] == DETAIL_EXTRA_COLUMNS:
        base_len = len(header) - len(DETAIL_EXTRA_COLUMNS)
        full_headers = header if FETCH_DETAIL_PAGES else header[:base_len]
    else:
        base_len = len(header)
        full_headers = header + DETAIL_EXTRA_COLUMNS if FETCH_DETAIL_PAGES else header

    seen: set[tuple[str, ...]] = set()
    for r in data_rows:
        padded = r + [""] * max(0, len(header) - len(r))
        base = tuple(padded[:base_len])
        if base:
            seen.add(base)

    return seen, len(data_rows), header, full_headers, True


def _parse_cell_date(value: str) -> datetime | None:
    s = (value or "").strip()[:10]
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def _page_reached_non_detail_start_date(
    rows: list[list[str]], header: list[str], anchor_date: date | None
) -> bool:
    if anchor_date is None:
        return True
    if not rows:
        return False
    try:
        date_ix = header.index("Date")
    except ValueError:
        date_ix = 0
    for row in rows:
        val = row[date_ix] if date_ix < len(row) else ""
        d = _parse_cell_date(val)
        if d is None:
            continue
        # Result pages are newest -> oldest, so start writing when we hit anchor or older.
        if d.date() <= anchor_date:
            return True
    return False


def _row_needs_detail_fill(row: list[str], detail_col_indices: list[int]) -> bool:
    for i in detail_col_indices:
        if i >= len(row) or not (row[i] or "").strip():
            return True
    return False


def _pad_row_to_len(row: list[str], n: int) -> list[str]:
    out = list(row)
    while len(out) < n:
        out.append("")
    return out


def write_csv_all(header: list[str], data_rows: list[list[str]]) -> None:
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(data_rows)


def _flush_detail_batch_csv(batch_index: int, header: list[str], rows: list[list[str]]) -> Path:
    DETAIL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DETAIL_DATA_DIR / f"detail_batch_{batch_index:04d}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def enrich_csv_details_date_range(context) -> tuple[int, list[str], dict]:
    """If OUT_CSV exists: backfill detail columns for rows with Date in [DETAIL_RANGE_START, DETAIL_RANGE_END],
    newest first (DETAIL_RANGE_END → DETAIL_RANGE_START). Writes main OUT_CSV after each row; every
    DETAIL_BATCH_ROWS completed fetches, appends a chunk CSV under DETAIL_DATA_DIR."""
    if not OUT_CSV.exists():
        return 0, [], {"reason": "no_csv"}
    if not OUT_CSV.read_text(encoding="utf-8-sig").strip():
        return 0, [], {"reason": "csv_empty"}

    with OUT_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        return 0, [h.strip() for h in rows[0]] if rows else [], {"reason": "no_data_rows"}

    header = [h.strip() for h in rows[0]]
    data_rows = [list(r) for r in rows[1:] if any((c or "").strip() for c in r)]

    has_detail_header = (
        len(header) >= len(DETAIL_EXTRA_COLUMNS)
        and header[-len(DETAIL_EXTRA_COLUMNS) :] == DETAIL_EXTRA_COLUMNS
    )
    if has_detail_header:
        full_header = header
        detail_start = len(header) - len(DETAIL_EXTRA_COLUMNS)
    else:
        full_header = header + DETAIL_EXTRA_COLUMNS
        detail_start = len(header)
        for r in data_rows:
            while len(r) < len(header):
                r.append("")
            r.extend([""] * len(DETAIL_EXTRA_COLUMNS))

    try:
        date_ix = full_header.index("Date")
    except ValueError:
        date_ix = 0

    detail_indices = list(range(detail_start, detail_start + len(DETAIL_EXTRA_COLUMNS)))

    for idx, r in enumerate(data_rows):
        data_rows[idx] = _pad_row_to_len(r, len(full_header))

    candidates: list[tuple[int, date]] = []
    for idx, r in enumerate(data_rows):
        d = _parse_cell_date(r[date_ix] if date_ix < len(r) else "")
        if d is None:
            continue
        rd = d.date()
        if not (DETAIL_RANGE_START <= rd <= DETAIL_RANGE_END):
            continue
        if not _row_needs_detail_fill(r, detail_indices):
            continue
        candidates.append((idx, rd))

    candidates.sort(key=lambda t: (t[1], t[0]), reverse=True)
    to_process: list[int] = [t[0] for t in candidates]

    if not to_process:
        log(
            f"[detail-range] No rows need detail in "
            f"{DETAIL_RANGE_START.isoformat()}…{DETAIL_RANGE_END.isoformat()}."
        )
        if not has_detail_header:
            write_csv_all(full_header, data_rows)
        return 0, full_header, {"reason": "nothing_to_do", "candidates": 0}

    log(
        f"[detail-range] {len(to_process)} row(s) to fetch "
        f"(dates {DETAIL_RANGE_END.isoformat()} → {DETAIL_RANGE_START.isoformat()}, newest first); "
        f"batches of {DETAIL_BATCH_ROWS} → {DETAIL_DATA_DIR.name}/"
    )

    consecutive_timeouts = 0
    fixed = 0
    batch_index = 1
    chunk_buffer: list[list[str]] = []

    iter_rows = (
        tqdm(to_process, desc="Detail fetch (2025 range)", unit="row", dynamic_ncols=True)
        if tqdm
        else to_process
    )

    for idx in iter_rows:
        r = data_rows[idx]
        r = _pad_row_to_len(r, len(full_header))
        data_rows[idx] = r
        detail_url = get_view_url_from_row(r)
        row_id = f"csv-{idx + 1}"
        detail_fields, timed_out = fetch_detail_fields(context, detail_url, row_id)
        if timed_out:
            consecutive_timeouts += 1
            if consecutive_timeouts >= 3:
                log(
                    f"      3 consecutive TimeoutError — pausing {DETAIL_TIMEOUT_BACKOFF_SEC // 60} minutes..."
                )
                time.sleep(DETAIL_TIMEOUT_BACKOFF_SEC)
                consecutive_timeouts = 0
        else:
            consecutive_timeouts = 0
        for j, col in enumerate(DETAIL_EXTRA_COLUMNS):
            r[detail_start + j] = detail_fields.get(col, "")
        fixed += 1
        write_csv_all(full_header, data_rows)
        chunk_buffer.append(list(r))
        if len(chunk_buffer) >= DETAIL_BATCH_ROWS:
            out_path = _flush_detail_batch_csv(batch_index, full_header, chunk_buffer)
            log(f"      [detail-range] wrote batch {batch_index}: {out_path.name} ({len(chunk_buffer)} rows)")
            chunk_buffer = []
            batch_index += 1

    if chunk_buffer:
        out_path = _flush_detail_batch_csv(batch_index, full_header, chunk_buffer)
        log(f"      [detail-range] wrote final batch {batch_index}: {out_path.name} ({len(chunk_buffer)} rows)")

    log(f"[detail-range] backfilled {fixed} row(s); main CSV updated.")
    meta = {
        "mode": "detail_enrich_date_range",
        "date_range_start": DETAIL_RANGE_START.isoformat(),
        "date_range_end": DETAIL_RANGE_END.isoformat(),
        "rows_fetched_this_run": fixed,
        "detail_batches_dir": str(DETAIL_DATA_DIR),
        "batch_rows": DETAIL_BATCH_ROWS,
        "headers": full_header,
        "output_csv": str(OUT_CSV),
    }
    return fixed, full_header, meta


def enrich_csv_missing_details(context) -> tuple[int, list[str], int | None]:
    """From first row with Date==CSV_ANCHOR_DATE through last row (file order), backfill empty detail columns."""
    if not OUT_CSV.exists():
        return 0, [], None
    if not OUT_CSV.read_text(encoding="utf-8-sig").strip():
        return 0, [], None

    with OUT_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        return 0, [h.strip() for h in rows[0]] if rows else [], None

    header = [h.strip() for h in rows[0]]
    data_rows = [list(r) for r in rows[1:] if any((c or "").strip() for c in r)]

    has_detail_header = (
        len(header) >= len(DETAIL_EXTRA_COLUMNS)
        and header[-len(DETAIL_EXTRA_COLUMNS) :] == DETAIL_EXTRA_COLUMNS
    )
    if has_detail_header:
        full_header = header
        detail_start = len(header) - len(DETAIL_EXTRA_COLUMNS)
    else:
        full_header = header + DETAIL_EXTRA_COLUMNS
        detail_start = len(header)
        for r in data_rows:
            while len(r) < len(header):
                r.append("")
            r.extend([""] * len(DETAIL_EXTRA_COLUMNS))

    try:
        date_ix = full_header.index("Date")
    except ValueError:
        date_ix = 0

    detail_indices = list(range(detail_start, detail_start + len(DETAIL_EXTRA_COLUMNS)))

    for idx, r in enumerate(data_rows):
        r = _pad_row_to_len(r, len(full_header))
        data_rows[idx] = r

    anchor_idx: int | None = None
    for idx, r in enumerate(data_rows):
        d = _parse_cell_date(r[date_ix] if date_ix < len(r) else "")
        if d is not None and d.date() == CSV_ANCHOR_DATE:
            anchor_idx = idx
            break

    if anchor_idx is None:
        log(f"[enrich] No row with Date={CSV_ANCHOR_DATE.isoformat()} found; nothing to do.")
        if not has_detail_header:
            write_csv_all(full_header, data_rows)
        return 0, full_header, None

    to_process: list[int] = []
    for idx in range(anchor_idx, len(data_rows)):
        r = data_rows[idx]
        if not _row_needs_detail_fill(r, detail_indices):
            continue
        to_process.append(idx)

    if not to_process:
        if not has_detail_header:
            write_csv_all(full_header, data_rows)
        return 0, full_header, anchor_idx

    log(
        f"[enrich] {len(to_process)} row(s) need detail "
        f"(file order from first {CSV_ANCHOR_DATE} at row {anchor_idx + 1} to EOF)"
    )
    consecutive_timeouts = 0
    fixed = 0

    iter_rows = (
        tqdm(to_process, desc="Detail backfill", unit="row", dynamic_ncols=True)
        if tqdm
        else to_process
    )

    for idx in iter_rows:
        r = data_rows[idx]
        r = _pad_row_to_len(r, len(full_header))
        data_rows[idx] = r
        detail_url = get_view_url_from_row(r)
        row_id = f"csv-{idx + 1}"
        detail_fields, timed_out = fetch_detail_fields(context, detail_url, row_id)
        if timed_out:
            consecutive_timeouts += 1
            if consecutive_timeouts >= 3:
                log(
                    f"      3 consecutive TimeoutError — pausing {DETAIL_TIMEOUT_BACKOFF_SEC // 60} minutes..."
                )
                time.sleep(DETAIL_TIMEOUT_BACKOFF_SEC)
                consecutive_timeouts = 0
        else:
            consecutive_timeouts = 0
        for j, col in enumerate(DETAIL_EXTRA_COLUMNS):
            r[detail_start + j] = detail_fields.get(col, "")
        fixed += 1
        write_csv_all(full_header, data_rows)

    log(f"[enrich] backfilled {fixed} row(s), CSV updated.")
    return fixed, full_header, anchor_idx


def main() -> None:
    args = parse_cli_args()
    apply_runtime_config(args)
    log(
        f"[config] country={TARGET_COUNTRY} "
        f"start_date={NON_DETAIL_START_FROM_DATE.isoformat()} detail={FETCH_DETAIL_PAGES}"
    )
    log("[1/4] Reading cookie header...")
    cookie_header = get_cookie_header(REQUEST_HEADER_FILE)
    cookies = parse_cookie_header(cookie_header, TARGET_URL)
    log(f"      Parsed cookies: {len(cookies)}")

    csv_nonempty = OUT_CSV.exists() and bool(OUT_CSV.read_text(encoding="utf-8-sig").strip())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        context.add_cookies(cookies)

        if FETCH_DETAIL_PAGES and csv_nonempty:
            log("[2/4] Launching browser (detail enrich: existing CSV, skip table scrape)")
            try:
                fixed, _full_header, detail_meta = enrich_csv_details_date_range(context)
                OUT_META.write_text(
                    json.dumps(detail_meta, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log(f"[3/4] Detail enrich finished; rows updated this run: {fixed}")
            finally:
                context.close()
                browser.close()
        else:
            log("[2/4] Launching browser (table scrape, no detail pages)...")
            page = context.new_page()
            try:
                page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)
                log(f"      Opened page: url={page.url!r} title={page.title()!r}")

                log(f"[3/4] Filters → Query → {TARGET_COUNTRY} → Exporter Transaction Details…")
                apply_search_filters(page)
                log(f"      [3f/7] Click {TARGET_COUNTRY} country")
                click_country_button(page, TARGET_COUNTRY)
                wait_table_loaded(page, step=f"after {TARGET_COUNTRY} click")
                log("      [3g/7] Switch to Exporter Transaction Details tab")
                click_exporter_transaction_details_tab(page)
                wait_table_loaded(page, step="after Exporter Transaction Details tab")

                log("      [3/4] UI steps finished; reading CSV resume state…")
                seen, table_header, all_rows = load_csv_table_resume_state()
                skip_pages = max(0, len(all_rows) // ROWS_PER_PAGE_ASSUMED - 1)
                log(f"      Resume: existing_rows={len(all_rows)}, skip_pages={skip_pages}")
                for i in range(skip_pages):
                    if not click_next_if_possible(page):
                        log(f"      skip: stopped early at {i + 1}/{skip_pages}")
                        break

                log("[4/4] Paginated table scrape…")
                page_idx = 0
                new_rows = 0
                started_collecting = NON_DETAIL_START_FROM_DATE is None
                while True:
                    page_idx += 1
                    wait_table_loaded(page)
                    headers, rows = read_active_table(page)
                    if not table_header:
                        if not headers:
                            log(f"      Page {page_idx}: no table header; stopping.")
                            break
                        table_header = list(headers)
                        log(f"      Table columns: {len(table_header)}")
                    elif headers and headers != table_header:
                        log(
                            f"      [warn] Page {page_idx}: thead differs from CSV header; "
                            "padding/truncating rows."
                        )

                    if not started_collecting:
                        started_collecting = _page_reached_non_detail_start_date(
                            rows,
                            table_header,
                            NON_DETAIL_START_FROM_DATE,
                        )
                        if started_collecting and NON_DETAIL_START_FROM_DATE is not None:
                            log(
                                f"      Reached anchor Date<={NON_DETAIL_START_FROM_DATE.isoformat()} "
                                f"at page {page_idx}; start collecting from this page."
                            )

                    if started_collecting:
                        ncols = len(table_header)
                        for row in rows:
                            padded = _pad_row_to_len(row, ncols)
                            if len(padded) > ncols:
                                padded = padded[:ncols]
                            key = tuple(padded)
                            if not any((c or "").strip() for c in key):
                                continue
                            if key in seen:
                                continue
                            seen.add(key)
                            all_rows.append(padded)
                            new_rows += 1
                    else:
                        log(
                            f"      Page {page_idx}: anchor Date<={NON_DETAIL_START_FROM_DATE.isoformat()} "
                            "not reached; continue clicking Next."
                        )

                    write_csv_all(table_header, all_rows)
                    log(
                        f"      Page {page_idx}: rows_on_page={len(rows)}, "
                        f"new_this_run={new_rows}, total_rows={len(all_rows)}"
                    )

                    if not click_next_if_possible(page):
                        break

                with suppress(Exception):
                    page.screenshot(path=str(OUT_SCREENSHOT), full_page=True)

                meta = {
                    "mode": "table_scrape",
                    "pages_visited": page_idx,
                    "rows_total": len(all_rows),
                    "new_rows_this_run": new_rows,
                    "skip_pages_applied": skip_pages,
                    "headers": table_header,
                    "output_csv": str(OUT_CSV),
                }
                OUT_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                log("      Saving debug snapshot from current browser session…")
                save_page_debug_snapshot(page)
                raise
            finally:
                context.close()
                browser.close()

    log("Done.")
    log(f"Saved CSV: {OUT_CSV}")
    log(f"Saved meta: {OUT_META}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"[ERROR] {type(exc).__name__}: {exc}")
        raise
