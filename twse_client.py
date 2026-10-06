import time
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) twse-daily-fetcher"}
TIMEOUT = 30
RETRIES = 3


def fetch_json(url: str, params: dict | None = None) -> list[dict]:
    """GET a TWSE JSON endpoint and return its list of records.

    Handles both openapi.twse.com.tw endpoints (bare JSON array) and the
    legacy www.twse.com.tw endpoints (dict with a "data"/"fields" pair).
    """
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            break
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            time.sleep(2 * attempt)
    else:
        raise RuntimeError(f"Failed to fetch {url}: {last_error}")

    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict) and "data" in payload and "fields" in payload:
        fields = payload["fields"]
        return [dict(zip(fields, row)) for row in payload["data"]]

    if isinstance(payload, dict) and payload.get("stat") != "OK":
        # e.g. requested date has no data yet (holiday, or not yet published)
        return []

    raise RuntimeError(f"Unrecognized response shape from {url}: {type(payload)}")


def remap_full(record: dict, mapping: dict) -> dict:
    """Build a clean record using only mapping's source->target keys, in
    target's names. Used where downstream SQL hardcodes column names
    (stock_price, stock_valuation, margin_trading, institutional_investors)
    so a TWSE-shaped table can also accept TPEx rows unchanged."""
    return {target: record.get(source, "") for source, target in mapping.items()}


def remap_partial(record: dict, mapping: dict) -> dict:
    """Rename only the given keys in place; all other keys pass through
    untouched. Used where extra source-specific columns are fine to keep
    (company_info, income/balance statements)."""
    out = dict(record)
    for source, target in mapping.items():
        if source in out:
            out[target] = out.pop(source)
    return out


def fetch_institutional_investors(iso_date: str) -> list[dict]:
    """三大法人買賣超日報 (T86) for a single trading day, all listed stocks."""
    url = "https://www.twse.com.tw/rwd/zh/fund/T86"
    params = {"response": "json", "date": iso_date.replace("-", ""), "selectType": "ALL"}
    records = fetch_json(url, params=params)
    # unlike openapi.twse.com.tw, this legacy endpoint formats every number
    # as a string with thousands-separator commas (e.g. "6,070,992"), which
    # silently truncates to 6.0 under SQLite's CAST(... AS REAL) - strip them
    for r in records:
        for k, v in r.items():
            if isinstance(v, str) and "," in v:
                r[k] = v.replace(",", "")
    return records
