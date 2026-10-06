"""Registry of TWSE (上市) + TPEx (上櫃) open-data endpoints pulled by
fetch_daily.py.

All endpoints are free and require no API key. Each covers every listed
company on its market in a single HTTP call - no per-stock looping anywhere.

TWSE and TPEx use different field names for equivalent data (e.g. TWSE's
STOCK_DAY_ALL has "Code"/"ClosingPrice", TPEx's mainboard_daily_close_quotes
has "SecuritiesCompanyCode"/"Close"). To keep every downstream query
(stock_lookup.py, industry_report.py, concept_report.py) market-agnostic,
TPEx datasets carry a `remap` of (mapping_dict, mode) that renames their
fields to match the TWSE table they merge into before upsert:
  - mode "full": build a clean record using ONLY the mapped fields - for
    tables downstream SQL hardcodes column names against (stock_price,
    stock_valuation, margin_trading, institutional_investors).
  - mode "partial": rename just the given keys, keep everything else - for
    tables where extra source-specific columns are harmless (company_info,
    income/balance statements).
"""

OPENAPI_BASE = "https://openapi.twse.com.tw/v1"
TPEX_BASE = "https://www.tpex.org.tw/openapi/v1"

# industry-segment suffixes TWSE splits financial-statement reports into
INDUSTRY_SUFFIXES = {
    "ci": "一般業",
    "fh": "金控業",
    "bd": "證券期貨業",
    "ins": "保險業",
    "mim": "異業",
    "basi": "金融業",
}

# --- 籌碼面 (chip / trading data), refreshed daily -------------------------
CHIP_DATASETS = [
    {
        "name": "stock_price",
        "table": "stock_price",
        "url": f"{OPENAPI_BASE}/exchangeReport/STOCK_DAY_ALL",
        "date_field": "Date",
        "date_is_roc": True,
        "key_cols": ["Code", "iso_date"],
    },
    {
        "name": "valuation_ratios",
        "table": "stock_valuation",
        "url": f"{OPENAPI_BASE}/exchangeReport/BWIBBU_ALL",
        "date_field": "Date",
        "date_is_roc": True,
        "key_cols": ["Code", "iso_date"],
    },
    {
        "name": "margin_trading",
        "table": "margin_trading",
        "url": f"{OPENAPI_BASE}/exchangeReport/MI_MARGN",
        "date_field": None,
        "key_cols": ["股票代號", "iso_date"],
    },
]

# --- TPEx (上櫃) field-name maps: source key -> target key, matching the
# TWSE-shaped table each merges into ------------------------------------
STOCK_PRICE_TPEX_MAP = {
    "Date": "Date", "SecuritiesCompanyCode": "Code", "CompanyName": "Name",
    "Open": "OpeningPrice", "High": "HighestPrice", "Low": "LowestPrice",
    "Close": "ClosingPrice", "Change": "Change",
    "TradingShares": "TradeVolume", "TransactionAmount": "TradeValue",
    "TransactionNumber": "Transaction",
}
VALUATION_TPEX_MAP = {
    "Date": "Date", "SecuritiesCompanyCode": "Code", "CompanyName": "Name",
    "PriceEarningRatio": "PEratio", "YieldRatio": "DividendYield",
    "PriceBookRatio": "PBratio",
}
MARGIN_TPEX_MAP = {
    "Date": "Date", "SecuritiesCompanyCode": "股票代號", "CompanyName": "股票名稱",
    "MarginPurchase": "融資買進", "MarginSales": "融資賣出",
    "CashRedemption": "融資現金償還",
    "MarginPurchaseBalancePreviousDay": "融資前日餘額",
    "MarginPurchaseBalance": "融資今日餘額", "MarginPurchaseQuota": "融資限額",
    "ShortConvering": "融券買進", "ShortSale": "融券賣出",
    "StockRedemption": "融券現券償還",
    "ShortSaleBalancePreviousDay": "融券前日餘額",
    "ShortSaleBalance": "融券今日餘額", "ShortSaleQuota": "融券限額",
    "Offsetting": "資券互抵", "Note": "註記",
}
# TPEx doesn't split dealer buy/sell into self-trading vs hedging like TWSE
# does; Dealers-* here is mapped to the "自行買賣" (self-trading) columns as
# the closest approximation, and 自營商買賣超股數 (dealer total, no suffix)
# is filled from the same number in fetch_daily.py after remap - the
# 避險 (hedging) columns are left empty for TPEx rows, not wrong data, TPEx
# just doesn't report that split.
INSTITUTIONAL_TPEX_MAP = {
    "Date": "Date", "SecuritiesCompanyCode": "證券代號", "CompanyName": "證券名稱",
    "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Buy":
        "外陸資買進股數(不含外資自營商)",
    " Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Sell":
        "外陸資賣出股數(不含外資自營商)",
    "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference":
        "外陸資買賣超股數(不含外資自營商)",
    "Foreign Dealers-Total Buy": "外資自營商買進股數",
    "Foreign Dealers-TotalSell": "外資自營商賣出股數",
    "ForeignDealers-Difference": "外資自營商買賣超股數",
    "SecuritiesInvestmentTrustCompanies-TotalBuy": "投信買進股數",
    "SecuritiesInvestmentTrustCompanies-TotalSell": "投信賣出股數",
    "SecuritiesInvestmentTrustCompanies-Difference": "投信買賣超股數",
    "Dealers-TotalBuy": "自營商買進股數(自行買賣)",
    "Dealers -TotalSell": "自營商賣出股數(自行買賣)",
    "Dealers-Difference": "自營商買賣超股數(自行買賣)",
    "TotalDifference": "三大法人買賣超股數",
}
COMPANY_INFO_TPEX_MAP = {
    "Date": "出表日期", "SecuritiesCompanyCode": "公司代號",
    "CompanyName": "公司名稱", "CompanyAbbreviation": "公司簡稱",
    "SecuritiesIndustryCode": "產業別", "Address": "住址",
    "Chairman": "董事長", "GeneralManager": "總經理",
    "DateOfListing": "上市日期",
}
FIN_STATEMENT_TPEX_MAP = {
    "Date": "出表日期", "SecuritiesCompanyCode": "公司代號",
    "CompanyName": "公司名稱", "Year": "年度", "Season": "季別",
}

TPEX_CHIP_DATASETS = [
    {
        "name": "stock_price_tpex",
        "table": "stock_price",
        "url": f"{TPEX_BASE}/tpex_mainboard_daily_close_quotes",
        "date_field": "Date",
        "date_is_roc": True,
        "key_cols": ["Code", "iso_date"],
        "remap": (STOCK_PRICE_TPEX_MAP, "full"),
    },
    {
        "name": "valuation_ratios_tpex",
        "table": "stock_valuation",
        "url": f"{TPEX_BASE}/tpex_mainboard_peratio_analysis",
        "date_field": "Date",
        "date_is_roc": True,
        "key_cols": ["Code", "iso_date"],
        "remap": (VALUATION_TPEX_MAP, "full"),
    },
    {
        "name": "margin_trading_tpex",
        "table": "margin_trading",
        "url": f"{TPEX_BASE}/tpex_mainboard_margin_balance",
        "date_field": "Date",
        "date_is_roc": True,
        "key_cols": ["股票代號", "iso_date"],
        "remap": (MARGIN_TPEX_MAP, "full"),
    },
    {
        "name": "institutional_investors_tpex",
        "table": "institutional_investors",
        "url": f"{TPEX_BASE}/tpex_3insti_daily_trading",
        "date_field": "Date",
        "date_is_roc": True,
        "key_cols": ["證券代號", "iso_date"],
        "remap": (INSTITUTIONAL_TPEX_MAP, "full"),
    },
]

# --- 基本面 (fundamentals) --------------------------------------------------
FUNDAMENTAL_DATASETS = [
    {
        "name": "company_info",
        "table": "company_info",
        "url": f"{OPENAPI_BASE}/opendata/t187ap03_L",
        "date_field": None,
        "key_cols": ["公司代號"],
    },
    {
        "name": "monthly_revenue",
        "table": "monthly_revenue",
        "url": f"{OPENAPI_BASE}/opendata/t187ap05_L",
        "date_field": None,
        "key_cols": ["公司代號", "資料年月"],
    },
    {
        "name": "dividend",
        "table": "dividend",
        "url": f"{OPENAPI_BASE}/opendata/t187ap45_L",
        "date_field": None,
        "key_cols": ["公司代號", "股利年度", "期別"],
    },
]

# income statement + balance sheet: one dataset per industry segment
for suffix in INDUSTRY_SUFFIXES:
    FUNDAMENTAL_DATASETS.append(
        {
            "name": f"income_statement_{suffix}",
            "table": f"income_statement_{suffix}",
            "url": f"{OPENAPI_BASE}/opendata/t187ap06_L_{suffix}",
            "date_field": None,
            "key_cols": ["公司代號", "年度", "季別"],
        }
    )
    FUNDAMENTAL_DATASETS.append(
        {
            "name": f"balance_sheet_{suffix}",
            "table": f"balance_sheet_{suffix}",
            "url": f"{OPENAPI_BASE}/opendata/t187ap07_L_{suffix}",
            "date_field": None,
            "key_cols": ["公司代號", "年度", "季別"],
        }
    )

TPEX_FUNDAMENTAL_DATASETS = [
    {
        "name": "company_info_tpex",
        "table": "company_info",
        "url": f"{TPEX_BASE}/mopsfin_t187ap03_O",
        "date_field": None,
        "key_cols": ["公司代號"],
        "remap": (COMPANY_INFO_TPEX_MAP, "partial"),
    },
    {
        # already Chinese field names identical to TWSE's t187ap05_L - no remap needed
        "name": "monthly_revenue_tpex",
        "table": "monthly_revenue",
        "url": f"{TPEX_BASE}/mopsfin_t187ap05_O",
        "date_field": None,
        "key_cols": ["公司代號", "資料年月"],
    },
]

for suffix in INDUSTRY_SUFFIXES:
    TPEX_FUNDAMENTAL_DATASETS.append(
        {
            "name": f"income_statement_tpex_{suffix}",
            "table": f"income_statement_{suffix}",
            "url": f"{TPEX_BASE}/mopsfin_t187ap06_O_{suffix}",
            "date_field": None,
            "key_cols": ["公司代號", "年度", "季別"],
            "remap": (FIN_STATEMENT_TPEX_MAP, "partial"),
        }
    )
    TPEX_FUNDAMENTAL_DATASETS.append(
        {
            "name": f"balance_sheet_tpex_{suffix}",
            "table": f"balance_sheet_{suffix}",
            "url": f"{TPEX_BASE}/mopsfin_t187ap07_O_{suffix}",
            "date_field": None,
            "key_cols": ["公司代號", "年度", "季別"],
            "remap": (FIN_STATEMENT_TPEX_MAP, "partial"),
        }
    )

ALL_DATASETS = CHIP_DATASETS + FUNDAMENTAL_DATASETS + TPEX_CHIP_DATASETS + TPEX_FUNDAMENTAL_DATASETS
