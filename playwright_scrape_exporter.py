from __future__ import annotations

import argparse
import csv
import json
import os
import re
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
# Country button clicked after Query (any country listed on the result page, any continent).
TARGET_COUNTRY = "CHINA"
TRADE_TYPE_CHOICES = ["Import", "Export"]
RESULT_TAB_CHOICES = ["importer", "exporter"]
# Languages in the "multilingual" popover next to Product Keywords.
KEYWORD_LANG_CHOICES = ["English", "Russia", "Spanish", "Portugal", "French", "Ukrainian"]

# Output paths; all of them are set in apply_runtime_config() from the CLI arguments.
OUT_CSV = Path(__file__).with_name("nbd_transaction.csv")
OUT_META = Path(__file__).with_name("nbd_transaction_meta.json")
OUT_SCREENSHOT = Path(__file__).with_name("nbd_transaction.png")
OUT_DEBUG_HTML = Path(__file__).with_name("nbd_transaction_debug.html")
OUT_DEBUG_SCREENSHOT = Path(__file__).with_name("nbd_transaction_debug.png")
DETAIL_EXTRA_COLUMNS = [
    "Description Of Goods",
    "USD Amount(FOB)",
    "Price in USD(FOB)",
    "Weight",
]

# Table scrape writes list columns only. Detail columns are filled by enrich_csv_missing_details()
# and enrich_csv_details_date_range() when CSV exists (main()).
FETCH_DETAIL_PAGES = False

# Search form (transaction search page). Defaults; every one has a CLI argument.
SEARCH_TRADE_TYPE = "Export"  # "Import" or "Export"
SEARCH_HS_CODE = ""  # empty = no HS filter
SEARCH_KEYWORDS = ""  # Product Keywords; empty = no keyword filter
SEARCH_KEYWORD_LANGS: list[str] = []  # subset of KEYWORD_LANG_CHOICES to tick
SEARCH_COMPANY_NAME = ""  # empty = do not filter by company
# Result tab: "importer" / "exporter" Transaction Details. None = follow SEARCH_TRADE_TYPE.
RESULT_TAB: str | None = None
# Default date range = the page's own default: the last year up to today.
_TODAY = date.today()
SEARCH_DATE_CLOSED = _TODAY.isoformat()
try:
    SEARCH_START_DATE = _TODAY.replace(year=_TODAY.year - 1).isoformat()
except ValueError:  # 29 Feb
    SEARCH_START_DATE = _TODAY.replace(year=_TODAY.year - 1, day=28).isoformat()
ROWS_PER_PAGE_ASSUMED = 10  # used with existing row count to skip pages when resuming
# Non-detail scrape starts writing once Date <= this anchor date (while clicking Next).
# Set to None to write from page 1.
NON_DETAIL_START_FROM_DATE = date(2025, 12, 31)

# CSV detail pass: find the first row with this Date, then process downward to end of file (file order).
CSV_ANCHOR_DATE = date(2025, 12, 30)

# After this many consecutive detail-page timeouts, pause (seconds).
DETAIL_TIMEOUT_BACKOFF_SEC = 1800

# If the site redirects to the login page, wait this long for a manual login in the browser.
LOGIN_WAIT_SEC = 900
# NBD_USERNAME / NBD_PASSWORD for pre-filling the login form (never committed).
ENV_FILE = Path(__file__).with_name(".env")

# When OUT_CSV exists and is non-empty, main() runs detail enrich only (table scrape is skipped).
# To run table scrape again, rename/remove OUT_CSV or empty the file first.
# Detail batches go to <output CSV name>_details/ by default (--detail-dir), one folder per
# search, so different searches never overwrite each other's batch files.
DETAIL_DATA_DIR = Path(__file__).with_name("detailed data")
DETAIL_BATCH_ROWS = 200
# Detail pass date range; defaults to the search range (--detail-start / --detail-end).
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


def _parse_choice(value: str, choices: list[str], name: str) -> str:
    v = (value or "").strip().lower()
    for c in choices:
        if c.lower() == v:
            return c
    raise argparse.ArgumentTypeError(f"{name} must be one of {choices}, got {value!r}")


def _parse_keyword_langs(value: str) -> list[str]:
    """Comma list like "russian,ukrainian" -> ["Russia", "Ukrainian"] (matched on first 4 letters)."""
    out: list[str] = []
    for part in (value or "").split(","):
        p = part.strip().lower()
        if not p:
            continue
        match = [c for c in KEYWORD_LANG_CHOICES if c.lower()[:4] == p[:4]]
        if not match:
            raise argparse.ArgumentTypeError(
                f"unknown keyword language {part.strip()!r}; choose from {KEYWORD_LANG_CHOICES}"
            )
        if match[0] not in out:
            out.append(match[0])
    return out


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape the NBD transaction search table (optionally enrich from detail pages)."
    )
    g = parser.add_argument_group("search form")
    g.add_argument(
        "--trade-type",
        default=SEARCH_TRADE_TYPE,
        type=lambda v: _parse_choice(v, TRADE_TYPE_CHOICES, "--trade-type"),
        help=f"'Import or Export' dropdown: import or export (default: {SEARCH_TRADE_TYPE.lower()}).",
    )
    g.add_argument("--hs-code", default=SEARCH_HS_CODE, help="HS Code (default: none).")
    g.add_argument(
        "--keywords",
        default=SEARCH_KEYWORDS,
        help='Product Keywords, in English, e.g. "thermal paper" (default: none).',
    )
    g.add_argument(
        "--keyword-langs",
        default=",".join(SEARCH_KEYWORD_LANGS),
        type=_parse_keyword_langs,
        help=(
            "Languages to tick in the 'multilingual' popover, comma separated: "
            f"{', '.join(KEYWORD_LANG_CHOICES)} (default: none)."
        ),
    )
    g.add_argument(
        "--company",
        default=SEARCH_COMPANY_NAME,
        help='Company Name, e.g. "HENAN JIANGHE PAPER CO., LTD." (default: none).',
    )
    g.add_argument(
        "--search-start",
        default=SEARCH_START_DATE,
        help=f"Start Date, YYYY-MM-DD (default: {SEARCH_START_DATE}).",
    )
    g.add_argument(
        "--search-end",
        default=SEARCH_DATE_CLOSED,
        help=f"End Date, YYYY-MM-DD (default: {SEARCH_DATE_CLOSED}).",
    )

    g = parser.add_argument_group("result page")
    g.add_argument(
        "--country",
        default=TARGET_COUNTRY,
        type=lambda v: v.strip().upper(),
        help=(
            "Country button to click after Query, as shown on the page, e.g. CHINA, VIETNAM, "
            f"UNITED KINGDOM; any continent tab (default: {TARGET_COUNTRY})."
        ),
    )
    g.add_argument(
        "--tab",
        default=None,
        type=lambda v: _parse_choice(v, RESULT_TAB_CHOICES, "--tab"),
        help=(
            "Result tab: importer or exporter (Transaction Details). "
            "Default: importer for --trade-type import, exporter for export."
        ),
    )
    g.add_argument(
        "--start-date",
        default=None,
        help=(
            "Start saving table rows once Date <= this value (YYYY-MM-DD). "
            "Default: --search-end, i.e. save everything in the search range."
        ),
    )

    g = parser.add_argument_group("output")
    g.add_argument(
        "--out",
        default=None,
        help=(
            "Output CSV file name or path (relative paths are next to this script). "
            "Default: built from the search, e.g. "
            "china_export_48119000_HENAN_JIANGHE_PAPER_CO_LTD_20251119-20261119.csv"
        ),
    )

    g = parser.add_argument_group("detail mode")
    g.add_argument(
        "--detail",
        default=False,
        type=parse_bool_arg,
        help=(
            "TRUE: also fetch detail pages. If the CSV for this search does not exist yet, "
            "the table is scraped first; if it exists, only details are fetched. "
            "FALSE: scrape the result table only (default)."
        ),
    )
    g.add_argument(
        "--detail-start",
        default=None,
        help="Only fetch details for rows with Date >= this (YYYY-MM-DD). Default: --search-start.",
    )
    g.add_argument(
        "--detail-end",
        default=None,
        help="Only fetch details for rows with Date <= this (YYYY-MM-DD). Default: --search-end.",
    )
    g.add_argument(
        "--detail-batch-rows",
        default=DETAIL_BATCH_ROWS,
        type=int,
        help=f"Write a batch CSV every N rows (default: {DETAIL_BATCH_ROWS}).",
    )
    g.add_argument(
        "--detail-dir",
        default=None,
        help="Folder for batch CSVs. Default: <output CSV name>_details next to the CSV.",
    )
    g.add_argument(
        "--detail-pause-min",
        default=DETAIL_TIMEOUT_BACKOFF_SEC // 60,
        type=int,
        help=(
            "Minutes to pause after 3 detail-page timeouts in a row "
            f"(default: {DETAIL_TIMEOUT_BACKOFF_SEC // 60})."
        ),
    )

    g = parser.add_argument_group("login")
    g.add_argument(
        "--login-wait-min",
        default=LOGIN_WAIT_SEC // 60,
        type=int,
        help=f"Minutes to wait for a manual login (default: {LOGIN_WAIT_SEC // 60}).",
    )
    return parser.parse_args()


def _parse_iso_date_arg(value: str, name: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"{name} must be YYYY-MM-DD, got {value!r}")


def _slug(text: str, max_len: int = 40) -> str:
    out = []
    for ch in text.upper():
        out.append(ch if ch.isascii() and ch.isalnum() else "_")
    slug = "_".join(part for part in "".join(out).split("_") if part)
    return slug[:max_len].rstrip("_")


def default_result_tab(trade_type: str) -> str:
    return "importer" if trade_type == "Import" else "exporter"


def build_default_output_csv(
    country: str,
    trade_type: str,
    hs_code: str,
    keywords: str,
    keyword_langs: list[str],
    company: str,
    tab: str,
    start: date,
    end: date,
) -> Path:
    """<country>_<import|export>[_<hs>][_KW_<keywords>[_<langs>]][_<company>][_<tab>tab]_<start>-<end>.csv"""
    parts = [_slug(country).lower() or "all", trade_type.lower()]
    if hs_code.strip():
        parts.append(hs_code.strip())
    kw = _slug(keywords, 30)
    if kw:
        parts.append(f"KW_{kw}")
        if keyword_langs:
            parts.append("-".join(lang[:3].upper() for lang in keyword_langs))
    company_slug = _slug(company)
    if company_slug:
        parts.append(company_slug)
    if tab != default_result_tab(trade_type):
        parts.append(f"{tab}tab")
    parts.append(f"{start:%Y%m%d}-{end:%Y%m%d}")
    return Path(__file__).with_name("_".join(parts) + ".csv")


def _resolve_path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else Path(__file__).parent / p


def apply_runtime_config(args: argparse.Namespace) -> None:
    global TARGET_COUNTRY, SEARCH_TRADE_TYPE, SEARCH_HS_CODE, SEARCH_KEYWORDS, SEARCH_KEYWORD_LANGS
    global SEARCH_COMPANY_NAME, SEARCH_START_DATE, SEARCH_DATE_CLOSED, RESULT_TAB
    global OUT_CSV, OUT_META, OUT_SCREENSHOT, OUT_DEBUG_HTML, OUT_DEBUG_SCREENSHOT
    global NON_DETAIL_START_FROM_DATE, FETCH_DETAIL_PAGES
    global DETAIL_DATA_DIR, DETAIL_RANGE_START, DETAIL_RANGE_END, DETAIL_BATCH_ROWS
    global DETAIL_TIMEOUT_BACKOFF_SEC, LOGIN_WAIT_SEC

    TARGET_COUNTRY = args.country
    SEARCH_TRADE_TYPE = args.trade_type
    SEARCH_HS_CODE = (args.hs_code or "").strip()
    SEARCH_KEYWORDS = (args.keywords or "").strip()
    SEARCH_KEYWORD_LANGS = list(args.keyword_langs)
    SEARCH_COMPANY_NAME = (args.company or "").strip()
    RESULT_TAB = args.tab or default_result_tab(SEARCH_TRADE_TYPE)
    if not (SEARCH_HS_CODE or SEARCH_KEYWORDS or SEARCH_COMPANY_NAME):
        raise SystemExit("Give at least one of --hs-code, --keywords, --company.")
    if SEARCH_KEYWORD_LANGS and not SEARCH_KEYWORDS:
        raise SystemExit("--keyword-langs needs --keywords.")

    search_start = _parse_iso_date_arg(args.search_start, "--search-start")
    search_end = _parse_iso_date_arg(args.search_end, "--search-end")
    if search_start > search_end:
        raise SystemExit(f"--search-start {search_start} is after --search-end {search_end}")
    SEARCH_START_DATE = search_start.isoformat()
    SEARCH_DATE_CLOSED = search_end.isoformat()

    if args.out:
        out = _resolve_path(args.out)
        if out.suffix.lower() != ".csv":
            out = out.with_name(out.name + ".csv")
    else:
        out = build_default_output_csv(
            TARGET_COUNTRY,
            SEARCH_TRADE_TYPE,
            SEARCH_HS_CODE,
            SEARCH_KEYWORDS,
            SEARCH_KEYWORD_LANGS,
            SEARCH_COMPANY_NAME,
            RESULT_TAB,
            search_start,
            search_end,
        )
    OUT_CSV = out
    stem = out.with_suffix("")
    OUT_META = stem.with_name(stem.name + "_meta.json")
    OUT_SCREENSHOT = stem.with_name(stem.name + ".png")
    OUT_DEBUG_HTML = stem.with_name(stem.name + "_debug.html")
    OUT_DEBUG_SCREENSHOT = stem.with_name(stem.name + "_debug.png")

    DETAIL_DATA_DIR = (
        _resolve_path(args.detail_dir) if args.detail_dir else stem.with_name(stem.name + "_details")
    )
    DETAIL_RANGE_START = (
        _parse_iso_date_arg(args.detail_start, "--detail-start") if args.detail_start else search_start
    )
    DETAIL_RANGE_END = (
        _parse_iso_date_arg(args.detail_end, "--detail-end") if args.detail_end else search_end
    )
    if DETAIL_RANGE_START > DETAIL_RANGE_END:
        raise SystemExit(f"detail range {DETAIL_RANGE_START} is after {DETAIL_RANGE_END}")
    if args.detail_batch_rows < 1:
        raise SystemExit("--detail-batch-rows must be >= 1")
    DETAIL_BATCH_ROWS = args.detail_batch_rows
    DETAIL_TIMEOUT_BACKOFF_SEC = max(0, args.detail_pause_min) * 60
    LOGIN_WAIT_SEC = max(1, args.login_wait_min) * 60

    if args.start_date:
        NON_DETAIL_START_FROM_DATE = _parse_iso_date_arg(args.start_date, "--start-date")
    else:
        NON_DETAIL_START_FROM_DATE = search_end
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
    """Cookie value from request_header.txt, or "" if the file or line is missing
    (the run then starts logged out and waits for a manual login)."""
    if not file_path.exists():
        return ""
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.lower().startswith("cookie:"):
            return line.split(":", 1)[1].strip()
    return ""


def is_login_page(page: Page) -> bool:
    return "/login" in (urlparse(page.url).path or "")


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader: KEY=VALUE lines, optional quotes, # comments."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def get_login_credentials() -> tuple[str, str]:
    """NBD_USERNAME / NBD_PASSWORD from the environment, else from .env next to this script."""
    env = load_env_file(ENV_FILE)
    user = os.environ.get("NBD_USERNAME") or env.get("NBD_USERNAME", "")
    password = os.environ.get("NBD_PASSWORD") or env.get("NBD_PASSWORD", "")
    return user.strip(), password


def prefill_login_form(page: Page, user: str, password: str) -> None:
    """Pick Email/Username login type, fill account and password, focus the captcha box.
    The captcha and the LOGIN click are left to the user."""
    login_type = "Email" if "@" in user else "Username"
    toggle = page.locator("button.dropdown-toggle").first
    toggle.wait_for(state="visible", timeout=30000)
    if toggle.inner_text().strip() != login_type:
        toggle.click()
        page.locator(".dropdown-menu a").filter(has_text=login_type).first.click()
        page.wait_for_timeout(300)
    if toggle.inner_text().strip() != login_type:
        raise RuntimeError(f"could not switch login type to {login_type}")
    account = page.locator(
        "input.form-control[type='text']:not([placeholder*='erification'])"
    ).first
    account.fill(user)
    page.locator("input.form-control[type='password']").first.fill(password)
    page.locator("input[placeholder*='erification']").first.click()
    log(f"      Login type '{login_type}' selected; account and password filled from .env.")


def wait_for_manual_login(page: Page) -> bool:
    """If the page is the login page, wait until the user logs in in the browser window.
    Account/password are pre-filled from .env when available. Returns True if a login
    happened, False if already logged in."""
    if not is_login_page(page):
        return False
    # Drop expired cookies so the login (and its captcha) starts from a fresh session.
    page.context.clear_cookies()
    page.reload(wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)

    user, password = get_login_credentials()
    prefilled = False
    if user and password:
        try:
            prefill_login_form(page, user, password)
            prefilled = True
        except Exception as exc:
            log(f"      [warn] Could not pre-fill the login form ({type(exc).__name__}: {exc}).")
    else:
        log(f"      No NBD_USERNAME / NBD_PASSWORD in {ENV_FILE.name}; log in by hand.")

    log("")
    log("=" * 70)
    log("  Not logged in (cookie missing or expired).")
    if prefilled:
        log("  In the browser window: type the verification code, then click LOGIN.")
    else:
        log("  In the browser window: pick Email/Username, enter account, password,")
        log("  verification code, then click LOGIN.")
    log(f"  Waiting up to {LOGIN_WAIT_SEC // 60} minutes; the scrape continues after login.")
    log("=" * 70)
    deadline = time.time() + LOGIN_WAIT_SEC
    while is_login_page(page):
        if time.time() > deadline:
            raise RuntimeError(f"No login within {LOGIN_WAIT_SEC // 60} minutes; stopping.")
        page.wait_for_timeout(2000)
    log(f"      Login detected (now at {page.url!r}).")
    page.wait_for_timeout(2000)
    return True


def save_cookies_to_header_file(context, file_path: Path) -> int:
    """Write the browser's current NBD cookies into the Cookie: line of request_header.txt,
    so the next run starts logged in. Other lines in the file are kept."""
    cookies = [c for c in context.cookies() if (c.get("domain") or "").lstrip(".").endswith("nbd.ltd")]
    if not cookies:
        return 0
    cookie_line = "Cookie: " + "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    lines = file_path.read_text(encoding="utf-8").splitlines() if file_path.exists() else []
    out_lines: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().lower().startswith("cookie:"):
            if not replaced:
                out_lines.append(cookie_line)
                replaced = True
            continue
        out_lines.append(line)
    if not replaced:
        out_lines.append(cookie_line)
    file_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return len(cookies)


def open_search_page_logged_in(page: Page, context) -> None:
    """Open TARGET_URL; if redirected to the login page, wait for a manual login,
    save the new cookies to request_header.txt and reopen TARGET_URL."""
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    if wait_for_manual_login(page):
        n = save_cookies_to_header_file(context, REQUEST_HEADER_FILE)
        log(f"      Saved {n} cookies to {REQUEST_HEADER_FILE.name} for the next run.")
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        if is_login_page(page):
            raise RuntimeError("Still on the login page after logging in; stopping.")
    log(f"      Opened page: url={page.url!r} title={page.title()!r}")


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


def _range_date_input(page: Page, placeholder: str):
    return page.locator(f"input.el-range-input[placeholder='{placeholder}']").first


def set_search_date_range(page: Page, start: str, end: str) -> None:
    """Fill Start/End Date. Current site: one Element UI range picker with two
    input.el-range-input (placeholders 'Start Date' / 'End Date'). Older layout: two
    input.form-control.date-picker (placeholders 'Start Date' / 'Date Closed')."""
    start_input = _range_date_input(page, "Start Date")
    end_input = _range_date_input(page, "End Date")
    if start_input.count() == 0:
        log("         → range picker not found; using legacy date-picker inputs")
        set_date_input_by_placeholder(page, "Start Date", start)
        set_date_input_by_placeholder(page, "Date Closed", end)
        return

    for label, loc, value in (("Start Date", start_input, start), ("End Date", end_input, end)):
        log(f"         → range picker: type {label} = {value!r}")
        loc.wait_for(state="visible", timeout=30000)
        loc.click()
        page.wait_for_timeout(400)  # picker panel mounts on focus
        loc.fill(value)  # fires 'input'
        loc.dispatch_event("change")  # picker parses typed text on 'change'
        page.wait_for_timeout(300)

    # Close the picker panel, then check the values stuck.
    page.keyboard.press("Escape")
    page.locator("xpath=//label[contains(normalize-space(), 'HS Code')]").first.click()
    page.wait_for_timeout(500)
    got_start = start_input.input_value().strip()
    got_end = end_input.input_value().strip()
    if (got_start, got_end) != (start, end):
        raise RuntimeError(
            f"Date range shows {got_start!r}..{got_end!r}, expected {start!r}..{end!r}"
        )
    log(f"         → range picker: confirmed {got_start} .. {got_end}")


def choose_trade_type(page: Page, trade_type: str) -> None:
    """Pick 'Import' or 'Export' in the 'Import or Export' dropdown and verify it."""
    log("         → waiting for Import/Export control (≤30s)…")
    select_input = page.locator(
        "xpath=//label[contains(normalize-space(), 'Import or Export')]/following::input[contains(@class, 'el-input__inner')][1]"
    ).first
    select_input.wait_for(state="visible", timeout=30000)
    select_input.click()
    log(f"         → waiting for {trade_type} option in dropdown…")
    option = page.locator(
        f"xpath=//div[contains(@class, 'el-select-dropdown')]//li[normalize-space()='{trade_type}']"
    ).first
    option.wait_for(state="visible", timeout=30000)
    option.click()
    page.wait_for_timeout(300)
    actual = select_input.input_value().strip()
    if actual != trade_type:
        raise RuntimeError(f"Import or Export shows {actual!r}, expected {trade_type!r}")
    log(f"         → {trade_type} selected.")


def set_product_keywords(page: Page, value: str) -> None:
    log("         → waiting for Product Keywords input (≤30s)…")
    input_locator = page.locator("input[placeholder*='English keyword']").first
    if input_locator.count() == 0:
        input_locator = page.locator(
            "xpath=//label[contains(normalize-space(), 'Product Keywords')]/following::input[1]"
        ).first
    input_locator.wait_for(state="visible", timeout=30000)
    input_locator.click()
    input_locator.fill(value)
    page.keyboard.press("Tab")
    actual = input_locator.input_value()
    if actual.strip() != value.strip():
        raise RuntimeError(f"Product Keywords input shows {actual!r}, expected {value!r}")
    log(f"         → Product Keywords set: {value!r}")


def set_keyword_languages(page: Page, langs: list[str]) -> None:
    """Tick exactly `langs` in the 'multilingual' popover (checkboxes English, Russia, ...)."""
    ref = page.locator("xpath=//*[@aria-describedby][contains(normalize-space(), 'multilingual')]").first
    ref.wait_for(state="attached", timeout=30000)
    pop_id = ref.get_attribute("aria-describedby")
    toggle_js = """([id, wanted]) => {
        const labels = [...document.getElementById(id).querySelectorAll('label.el-checkbox')];
        for (const l of labels) {
            const want = wanted.includes(l.innerText.trim());
            const checked = l.querySelector('input').checked;
            if (want !== checked) l.querySelector('input').click();
        }
    }"""
    page.evaluate(toggle_js, [pop_id, langs])
    page.wait_for_timeout(400)
    state = page.evaluate(
        """id => [...document.getElementById(id).querySelectorAll('label.el-checkbox')]
            .map(l => [l.innerText.trim(), l.querySelector('input').checked])""",
        pop_id,
    )
    ticked = sorted(name for name, checked in state if checked)
    if ticked != sorted(langs):
        raise RuntimeError(f"multilingual shows {ticked}, expected {sorted(langs)}")
    log(f"         → Keyword languages ticked: {', '.join(ticked)}")


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


COUNTRY_BUTTONS = "[id^='continent_'] button"


def click_country_button(page: Page, country: str) -> None:
    """Click the country button (e.g. 'CHINA (46)') in whichever continent tab holds it.
    Panes look like <div id="continent_2" class="tab-pane">…<button>UNITED KINGDOM (3077)</button>,
    with tab links Asia / Europe / North America / … pointing at #continent_N."""
    target_country = (country or "").strip().upper()
    if not target_country:
        raise ValueError("country must be non-empty")
    log(f"         → looking for {target_country} button in all continent tabs (≤120s)…")
    page.locator(COUNTRY_BUTTONS).first.wait_for(state="attached", timeout=120000)
    # Exact name before the count, so CHINA does not match e.g. "TAIWAN, CHINA (5)".
    pattern = re.compile(rf"^\s*{re.escape(target_country)}\s*\(", re.IGNORECASE)
    btn = page.locator(COUNTRY_BUTTONS).filter(has_text=pattern).first
    if btn.count() == 0:
        available = [t.strip() for t in page.locator(COUNTRY_BUTTONS).all_inner_texts() if t.strip()]
        raise RuntimeError(
            f"No {target_country!r} button in the results. Available: {', '.join(available[:60])}"
        )
    if not btn.is_visible():
        pane_id = btn.evaluate("b => b.closest(\"[id^='continent_']\").id")
        tab_link = page.locator(
            f"[href='#{pane_id}'], [data-target='#{pane_id}'], [aria-controls='{pane_id}']"
        ).first
        log(f"         → {target_country} is under '{tab_link.inner_text().strip()}', opening that tab…")
        tab_link.click()
        page.wait_for_timeout(600)
    btn.wait_for(state="visible", timeout=30000)
    log(f"         → {btn.inner_text().strip()} button visible, clicking…")
    btn.click()
    log(f"         → {target_country} clicked.")


def click_transaction_details_tab(page: Page, which: str) -> None:
    """which: 'importer' or 'exporter' -> '<Importer|Exporter> Transaction Details' tab."""
    label = f"{which.capitalize()} Transaction Details"
    log(f"         → waiting for '{label}' tab (≤30s)…")
    tab = page.locator(
        "xpath=(//*[contains(@class,'el-tabs__item') or contains(@class,'nav-link')]"
        f"[contains(., '{label}')])[1]"
    ).first
    tab.wait_for(state="visible", timeout=30000)
    log("         → tab visible, clicking…")
    tab.click()
    log(f"         → {label} tab clicked.")


def set_company_name(page: Page, value: str) -> None:
    log("         → waiting for Company Name input (≤30s)…")
    # The company input has placeholder "eg WIKO USA INC. Just enter WIKO".
    input_locator = page.locator("input[placeholder*='Just enter']").first
    if input_locator.count() == 0:
        input_locator = page.locator(
            "xpath=//label[contains(normalize-space(), 'Company Name')]/following::input[1]"
        ).first
    input_locator.wait_for(state="visible", timeout=30000)
    input_locator.click()
    input_locator.fill(value)
    page.keyboard.press("Tab")
    actual = input_locator.input_value()
    if actual.strip() != value.strip():
        raise RuntimeError(f"Company Name input shows {actual!r}, expected {value!r}")
    log(f"         → Company Name set: {value!r}")


def apply_search_filters(page: Page) -> None:
    log(f"      [3a/7] Set Import or Export to {SEARCH_TRADE_TYPE}")
    choose_trade_type(page, SEARCH_TRADE_TYPE)
    if SEARCH_HS_CODE:
        log("      [3b/7] Fill HS Code")
        set_input_value(page, "HS Code", SEARCH_HS_CODE)
    else:
        log("      [3b/7] HS Code: none")
    if SEARCH_KEYWORDS:
        log("      [3c/7] Fill Product Keywords")
        set_product_keywords(page, SEARCH_KEYWORDS)
        if SEARCH_KEYWORD_LANGS:
            set_keyword_languages(page, SEARCH_KEYWORD_LANGS)
    else:
        log("      [3c/7] Product Keywords: none")
    if SEARCH_COMPANY_NAME:
        log("      [3d/7] Fill Company Name")
        set_company_name(page, SEARCH_COMPANY_NAME)
    else:
        log("      [3d/7] Company Name: none")
    log("      [3e/7] Fill Start Date / End Date")
    set_search_date_range(page, SEARCH_START_DATE, SEARCH_DATE_CLOSED)
    log("      [3f/7] Submit Query")
    click_query_button(page)
    page.wait_for_timeout(2000)
    wait_table_loaded(page, step="after Query")
    # Strong check: country buttons appear (in some continent tab) after Query.
    page.locator(COUNTRY_BUTTONS).first.wait_for(state="attached", timeout=60000)
    log("         → Query effect confirmed: country buttons are present.")


def load_csv_table_resume_state() -> tuple[set[tuple[str, ...]], list[str], list[list[str]], list[str]]:
    """Load existing CSV for dedup + resume.
    Returns (seen table-column keys, table header, full rows, trailing detail header).
    Rows keep their detail columns, so a table rerun never drops already fetched details."""
    if not OUT_CSV.exists():
        return set(), [], [], []
    text = OUT_CSV.read_text(encoding="utf-8-sig")
    if not text.strip():
        return set(), [], [], []

    with OUT_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return set(), [], [], []

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

    extra_header = header[base_len:]
    seen: set[tuple[str, ...]] = set()
    full_rows: list[list[str]] = []
    for r in data_rows:
        padded = _pad_row_to_len(r, len(header))[: len(header)]
        base_tuple = tuple(padded[:base_len])
        if any(c.strip() for c in base_tuple):
            seen.add(base_tuple)
        full_rows.append(padded)

    return seen, table_header, full_rows, extra_header


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


def _next_detail_batch_index() -> int:
    existing = []
    for p in DETAIL_DATA_DIR.glob("detail_batch_*.csv"):
        with suppress(ValueError):
            existing.append(int(p.stem.rsplit("_", 1)[1]))
    return max(existing, default=0) + 1


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
    # Continue numbering after existing batch files so a rerun never overwrites earlier batches.
    batch_index = _next_detail_batch_index()
    chunk_buffer: list[list[str]] = []

    iter_rows = (
        tqdm(to_process, desc="Detail fetch", unit="row", dynamic_ncols=True)
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


def scrape_table(page: Page, context) -> dict:
    """Search form -> Query -> country -> Transaction Details tab -> page through the table,
    appending new rows to OUT_CSV. Keeps detail columns of rows already in the CSV."""
    tab_label = f"{RESULT_TAB.capitalize()} Transaction Details"
    log(f"[3/4] Filters → Query → {TARGET_COUNTRY} → {tab_label}…")
    apply_search_filters(page)
    log(f"      [3g/7] Click {TARGET_COUNTRY} country")
    click_country_button(page, TARGET_COUNTRY)
    wait_table_loaded(page, step=f"after {TARGET_COUNTRY} click")
    log(f"      [3h/7] Switch to {tab_label} tab")
    click_transaction_details_tab(page, RESULT_TAB)
    wait_table_loaded(page, step=f"after {tab_label} tab")

    log("      [3/4] UI steps finished; reading CSV resume state…")
    seen, table_header, all_rows, extra_header = load_csv_table_resume_state()
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
                padded = _pad_row_to_len(row, ncols)[:ncols]
                key = tuple(padded)
                if not any((c or "").strip() for c in key):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                all_rows.append(padded + [""] * len(extra_header))
                new_rows += 1
        else:
            log(
                f"      Page {page_idx}: anchor Date<={NON_DETAIL_START_FROM_DATE.isoformat()} "
                "not reached; continue clicking Next."
            )

        write_csv_all(table_header + extra_header, all_rows)
        log(
            f"      Page {page_idx}: rows_on_page={len(rows)}, "
            f"new_this_run={new_rows}, total_rows={len(all_rows)}"
        )

        if not click_next_if_possible(page):
            break

    with suppress(Exception):
        page.screenshot(path=str(OUT_SCREENSHOT), full_page=True)

    return {
        "mode": "table_scrape",
        "pages_visited": page_idx,
        "rows_total": len(all_rows),
        "new_rows_this_run": new_rows,
        "skip_pages_applied": skip_pages,
        "headers": table_header + extra_header,
        "output_csv": str(OUT_CSV),
    }


def main() -> None:
    args = parse_cli_args()
    apply_runtime_config(args)
    log(
        f"[config] trade_type={SEARCH_TRADE_TYPE} hs_code={SEARCH_HS_CODE or '(none)'} "
        f"keywords={SEARCH_KEYWORDS or '(none)'}"
        + (f" [{', '.join(SEARCH_KEYWORD_LANGS)}]" if SEARCH_KEYWORD_LANGS else "")
        + f" company={SEARCH_COMPANY_NAME or '(none)'} "
        f"search={SEARCH_START_DATE}..{SEARCH_DATE_CLOSED}"
    )
    log(
        f"[config] country={TARGET_COUNTRY} tab={RESULT_TAB} "
        f"start_date={NON_DETAIL_START_FROM_DATE.isoformat()} detail={FETCH_DETAIL_PAGES}"
    )
    if FETCH_DETAIL_PAGES:
        log(
            f"[config] detail range={DETAIL_RANGE_START}..{DETAIL_RANGE_END} "
            f"batch_rows={DETAIL_BATCH_ROWS} dir={DETAIL_DATA_DIR}"
        )
    log(f"[config] output CSV: {OUT_CSV}")

    csv_nonempty = OUT_CSV.exists() and bool(OUT_CSV.read_text(encoding="utf-8-sig").strip())
    # --detail TRUE on an existing CSV only enriches it; otherwise scrape the table first.
    run_table = not (FETCH_DETAIL_PAGES and csv_nonempty)

    log("[1/4] Reading cookie header...")
    cookie_header = get_cookie_header(REQUEST_HEADER_FILE)
    cookies = parse_cookie_header(cookie_header, TARGET_URL) if cookie_header else []
    if cookies:
        log(f"      Parsed cookies: {len(cookies)}")
    else:
        log(f"      No cookie in {REQUEST_HEADER_FILE.name}; you will be asked to log in.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        meta: dict = {}
        try:
            if run_table:
                log("[2/4] Launching browser (table scrape)...")
            else:
                log("[2/4] Launching browser (CSV exists: detail enrich only, table scrape skipped)")
            open_search_page_logged_in(page, context)

            if run_table:
                try:
                    meta = scrape_table(page, context)
                except Exception:
                    log("      Saving debug snapshot from current browser session…")
                    save_page_debug_snapshot(page)
                    raise
                OUT_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

            if FETCH_DETAIL_PAGES:
                log("[detail] Fetching detail pages…")
                fixed, _full_header, detail_meta = enrich_csv_details_date_range(context)
                meta = {"table_scrape": meta, "detail_enrich": detail_meta} if run_table else detail_meta
                OUT_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                log(f"[detail] Detail enrich finished; rows updated this run: {fixed}")
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
