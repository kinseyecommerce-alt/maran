"""
nifty100.py
Complete Nifty 100 constituent list (Nifty 50 + Nifty Next 50), plus the
next 400 names that make up the full Nifty 500 (NIFTY_NEXT_400 / NIFTY_500).
Used as the default watchlist when USE_NIFTY100_WATCHLIST=true.
"""
from __future__ import annotations

# ── Nifty 50 ──────────────────────────────────────────────────────────────────
NIFTY_50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BPCL", "BHARTIARTL",
    "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC", "INDUSINDBK",
    "INFY", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "NESTLEIND", "NTPC", "ONGC", "POWERGRID",
    "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
    # TATAMOTORS demerged (Oct 2025) into TMPV (passenger) + TMCV (commercial);
    # ZOMATO renamed to ETERNAL — verified against the live Kite instrument dump.
    "TATACONSUM", "TMPV", "TATASTEEL", "TCS", "TECHM",
    "TITAN", "TRENT", "ULTRACEMCO", "WIPRO", "ETERNAL",
]

# ── Nifty Next 50 ─────────────────────────────────────────────────────────────
NIFTY_NEXT_50 = [
    "ABB", "ADANIGREEN", "ADANIPOWER", "AMBUJACEM", "AUROPHARMA",
    "BANKBARODA", "BERGEPAINT", "BOSCHLTD", "CANBK", "CHOLAFIN",
    "COLPAL", "DABUR", "DMART", "DIVISLAB", "DLF",
    "GAIL", "GODREJCP", "HAL", "HAVELLS", "HINDPETRO",
    "ICICIGI", "ICICIPRULI", "INDHOTEL", "IOC", "IRCTC",
    # LTIM has no NSE listing in the Kite dump any more (dropped); TMCV is the
    # Tata Motors commercial-vehicles entity; MCDOWELL-N renamed to UNITDSPR.
    "JIOFIN", "JUBLFOOD", "LODHA", "TMCV", "LUPIN",
    "UNITDSPR", "MOTHERSON", "MPHASIS", "NAUKRI", "NHPC",
    "NMDC", "OFSS", "PAGEIND", "PAYTM", "PETRONET",
    "PIDILITIND", "PIIND", "RECLTD", "SIEMENS", "TATAPOWER",
    "TORNTPHARM", "TVSMOTOR", "VBL", "VEDL", "ZYDUSLIFE",
]

# ── Combined ──────────────────────────────────────────────────────────────────
NIFTY_100 = NIFTY_50 + NIFTY_NEXT_50

# ── Nifty 500 ranks 101-500 ────────────────────────────────────────────────────
# Sourced from NSE's official constituent file (archives.nseindia.com/content/
# indices/ind_nifty500list.csv, fetched 2026-07-06 — see data/nifty500_
# constituents.csv), diffed against NIFTY_100 above (zero overlap, zero
# transcription — machine-diffed from the CSV). NSE reconstitutes the index
# semi-annually (Mar/Sep) — re-fetch the CSV and re-diff around those dates.
NIFTY_NEXT_400 = [
    "360ONE", "3MINDIA", "ACC", "ACMESOLAR", "AIAENG", "APLAPOLLO",
    "AUBANK", "AWL", "AADHARHFC", "AARTIIND", "AAVAS", "ABBOTINDIA",
    "ACE", "ACUTAAS", "ADANIENSOL", "ATGL", "ABCAPITAL", "ABFRL",
    "ABLBL", "ABREL", "ABSLAMC", "CPPLUS", "AEGISLOG", "AEGISVOPAK",
    "AFCONS", "AFFLE", "AJANTPHARM", "ALKEM", "ABDL", "ARE&M",
    "AMBER", "ANANDRATHI", "ANANTRAJ", "ANGELONE", "ANTHEM", "ANURAS",
    "APARINDS", "APOLLOTYRE", "APTUS", "ASAHIINDIA", "ASHOKLEY", "ASTERDM",
    "ASTRAL", "ATHERENERG", "ATUL", "AIIL", "BEML", "BLS",
    "BSE", "BAJAJHLDNG", "BAJAJHFL", "BALKRISIND", "BALRAMCHIN", "BANDHANBNK",
    "BANKINDIA", "MAHABANK", "BATAINDIA", "BAYERCROP", "BELRISE", "BDL",
    "BEL", "BHARATFORG", "BHEL", "BHARTIHEXA", "BIKAJI", "GROWW",
    "BIOCON", "BSOFT", "BLUEDART", "BLUEJET", "BLUESTARCO", "BBTC",
    "FIRSTCRY", "BRIGADE", "MAPMYINDIA", "CCL", "CESC", "CGPOWER",
    "CIEINDIA", "CRISIL", "CANFINHOME", "CANHLIFE", "CAPLIPOINT", "CGCL",
    "CARBORUNIV", "CARTRADE", "CASTROLIND", "CEATLTD", "CEMPRO", "CENTRALBK",
    "CDSL", "CHALET", "CHAMBLFERT", "CHENNPETRO", "CHOICEIN", "CHOLAHLDNG",
    "CUB", "CLEAN", "COCHINSHIP", "COFORGE", "COHANCE", "CAMS",
    "CONCORDBIO", "CONCOR", "COROMANDEL", "CRAFTSMAN", "CREDITACC", "CROMPTON",
    "CUMMINSIND", "CYIENT", "DCMSHRIRAM", "DOMS", "DALBHARAT", "DATAPATTNS",
    "DEEPAKFERT", "DEEPAKNTR", "DELHIVERY", "DEVYANI", "DIXON", "LALPATHLAB",
    "EIDPARRY", "EIHOTEL", "ELECON", "ELGIEQUIP", "EMAMILTD", "EMCURE",
    "EMMVEE", "ENDURANCE", "ENGINERSIN", "ERIS", "ESCORTS", "EXIDEIND",
    "NYKAA", "FEDERALBNK", "FACT", "FINCABLES", "FSL", "FIVESTAR",
    "FORCEMOT", "FORTIS", "GVT&D", "GMRAIRPORT", "GABRIEL", "GALLANTT",
    "GRSE", "GICRE", "GILLETTE", "GLAND", "GLAXO", "GLENMARK",
    "MEDANTA", "GODIGIT", "GPIL", "GODFRYPHLP", "GODREJIND", "GODREJPROP",
    "GRANULES", "GRAPHITE", "GRAVITA", "GESHIP", "FLUOROCHEM", "GMDCLTD",
    "HEG", "HBLENGINE", "HDBFS", "HDFCAMC", "HFCL", "HEXT",
    "HSCL", "HINDCOPPER", "HINDZINC", "POWERINDIA", "HOMEFIRST", "HONASA",
    "HONAUT", "HUDCO", "HYUNDAI", "ICICIAMC", "IDBI", "IDFCFIRSTB",
    "IFCI", "IIFL", "IRB", "IRCON", "ITCHOTELS", "ITI",
    "INDGN", "INDIACEM", "INDIAMART", "INDIANB", "IEX", "IOB",
    "IRFC", "IREDA", "IGL", "INDUSTOWER", "INOXWIND", "INTELLECT",
    "INDIGO", "IGIL", "IKS", "IPCALAB", "JBCHEPHARM", "JKCEMENT",
    "JBMA", "JKTYRE", "JMFINANCIL", "JSWCEMENT", "JSWDULUX", "JSWENERGY",
    "JSWINFRA", "JAINREC", "JPPOWER", "J&KBANK", "JINDALSAW", "JSL",
    "JINDALSTEL", "JUBLINGREA", "JUBLPHARMA", "JWL", "JYOTICNC", "KPRMILL",
    "KEI", "KPITTECH", "KAJARIACER", "KPIL", "KALYANKJIL", "KARURVYSYA",
    "KAYNES", "KEC", "KFINTECH", "KIRLOSENG", "KIMS", "LTF",
    "LTTS", "LGEINDIA", "LICHSGFIN", "LTFOODS", "LTM", "LATENTVIEW",
    "LAURUSLABS", "THELEELA", "LEMONTREE", "LENSKART", "LICI", "LINDEINDIA",
    "LLOYDSME", "MMTC", "MRF", "MGL", "M&MFIN", "MANAPPURAM",
    "MRPL", "MANKIND", "MARICO", "MFSL", "MAXHEALTH", "MAZDOCK",
    "MEESHO", "MINDACORP", "MSUMI", "MOTILALOFS", "MCX", "MUTHOOTFIN",
    "NATCOPHARM", "NBCC", "NCC", "NLCINDIA", "NSLNISP", "NTPCGREEN",
    "NH", "NATIONALUM", "NAVA", "NAVINFLUOR", "NETWEB", "NEULANDLAB",
    "NEWGEN", "NAM-INDIA", "NIVABUPA", "NUVAMA", "NUVOCO", "OBEROIRLTY",
    "OIL", "OLAELEC", "OLECTRA", "ONESOURCE", "POLICYBZR", "PCBL",
    "PGEL", "PNBHOUSING", "PTCIL", "PVRINOX", "PARADEEP", "PATANJALI",
    "PERSISTENT", "PFIZER", "PHOENIXLTD", "PWL", "PINELABS", "PIRAMALFIN",
    "PPLPHARMA", "POLYMED", "POLYCAB", "POONAWALLA", "PFC", "PREMIERENE",
    "PRESTIGE", "PNB", "RRKABEL", "RBLBANK", "RHIM", "RITES",
    "RADICO", "RVNL", "RAILTEL", "RAINBOW", "RKFORGE", "REDINGTON",
    "RPOWER", "SBFC", "SBICARD", "SJVN", "SRF", "SAGILITY",
    "SAILIFE", "SAMMAANCAP", "SAPPHIRE", "SARDAEN", "SAREGAMA", "SCHAEFFLER",
    "SCHNEIDER", "SCI", "SHREECEM", "SHYAMMETL", "ENRIN", "SIGNATURE",
    "SOBHA", "SOLARINDS", "SONACOMS", "SONATSOFTW", "STARHEALTH", "SAIL",
    "SUMICHEM", "SUNTV", "SUNDARMFIN", "SUPREMEIND", "SPLPETRO", "SUZLON",
    "SWANCORP", "SWIGGY", "SYNGENE", "SYRMA", "TBOTEK", "TATACAP",
    "TATACHEM", "TATACOMM", "TATAELXSI", "TATAINVEST", "TATATECH", "TTML",
    "TECHNOE", "TEGA", "TEJASNET", "TENNIND", "NIACL", "RAMCOCEM",
    "THERMAX", "TIMKEN", "TITAGARH", "TORNTPOWER", "TARIL", "TRAVELFOOD",
    "TRIDENT", "TRITURBINE", "TIINDIA", "UCOBANK", "UNOMINDA", "UPL",
    "UTIAMC", "UNIONBANK", "UBL", "URBANCO", "USHAMART", "VTL",
    "VIJAYA", "VMM", "IDEA", "VOLTAS", "WAAREEENER", "WELCORP",
    "WELSPUNLIV", "WHIRLPOOL", "WOCKPHARMA", "YESBANK", "ZFCVINDIA", "ZEEL",
    "ZENTEC", "ZENSARTECH", "ZYDUSWELL", "ECLERX",
]

NIFTY_500 = NIFTY_100 + NIFTY_NEXT_400

# Nifty 200 ≈ Nifty 100 + the next 100 largest names. (The official index is
# Nifty 100 + Nifty Midcap 100; NIFTY_NEXT_400 isn't market-cap ranked, so this
# is a close large/mid-cap approximation — swap in an exact constituents list if
# precise membership is required.)
NIFTY_200 = NIFTY_100 + NIFTY_NEXT_400[:100]

# Strategy-suitability hints (used to pre-filter per-strategy watchlists)
# All cash-equity strategies now scan the full Nifty 500 — the historical_learner
# backtest gate (per-symbol win rate/sharpe) is what actually prunes illiquid or
# no-edge names before they trade, not this list, so widening it costs nothing
# but candidate breadth. F&O stays capped to NIFTY_50 below: most Nifty 500
# names have no listed futures/options contract at all.
SCALPING_UNIVERSE = NIFTY_500

FNO_UNIVERSE = NIFTY_200  # Single production agent trades the Nifty 200 universe

SWING_UNIVERSE = NIFTY_500  # Full Nifty 500 for swing (hold overnight)

INTRADAY_UNIVERSE = NIFTY_500  # Full Nifty 500 for intraday MIS

# ── Index symbols — subscribed for price/chart data, NOT traded directly ───────
INDEX_SYMBOLS = [
    {"symbol": "NIFTY",     "exchange": "NSE"},
    {"symbol": "BANKNIFTY", "exchange": "NSE"},
]

# Watchlist dict format expected by agents
def as_watchlist(symbols: list[str], exchange: str = "NSE") -> list[dict]:
    return [{"symbol": s, "exchange": exchange} for s in symbols]

def get_strategy_watchlist(strategy: str) -> list[dict]:
    mapping = {
        "intraday":  INTRADAY_UNIVERSE,
        "scalping":  SCALPING_UNIVERSE,
        "options":       FNO_UNIVERSE,
        "swing":     SWING_UNIVERSE,
    }
    return as_watchlist(mapping.get(strategy, NIFTY_500))
