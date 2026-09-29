# -*- coding: utf-8 -*-
"""
MIRAE-ELS-CHECK collector
- Discovers Mirae Asset Securities public ELS/ELB products (ISIN KR6MD0...)
- Parses product conditions from the official product detail page
- Pulls underlying prices (Yahoo Finance, Naver fallback for KR)
- Computes early-redemption judgment per product
- Writes data/els.json (site data) and data/status.json (run log)

Run: python scripts/collect.py            (incremental)
     FULL_SCAN=1 python scripts/collect.py (wide ISIN scan)
"""
import os, re, json, time, sys, math, traceback
from datetime import datetime, date, timedelta, timezone

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import redeemed as redeemed_mod
except Exception:
    redeemed_mod = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).date()

BASE = "https://securities.miraeasset.com"
DETAIL_URL = BASE + "/hks/hks4023/p02.do?item_cd={isin}"
LIST_PAGES = [BASE + "/hks/hks4023/n01.do", BASE + "/hks/hks4022/n02_2.do"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": BASE + "/hks/hks4023/n01.do",
}

# ---- scan controls (env overrides) ----
FULL_SCAN = os.environ.get("FULL_SCAN", "0") == "1"
SCAN_START_SEQ = int(os.environ.get("SCAN_START_SEQ", "5700"))   # ~2023-08 issue region (3y products still alive)
MAX_PROBES = int(os.environ.get("MAX_PROBES", "1800" if FULL_SCAN else "260"))
FORWARD_MISS_LIMIT = int(os.environ.get("FORWARD_MISS_LIMIT", "400" if FULL_SCAN else "150"))
REQ_SLEEP = float(os.environ.get("REQ_SLEEP", "0.25"))

PRODUCTS_FILE = os.path.join(DATA, "products.json")   # parsed product cache (by ISIN)
REDEEMED_FILE = os.path.join(DATA, "redeemed.json")   # redemption list from KSD SEIBro (by ISIN / CODE:xxxxx)
EXCLUDED_FILE = os.path.join(DATA, "excluded.json")   # permanently excluded ISINs {isin: {reason, date}}
SCAN_FILE = os.path.join(DATA, "scan_state.json")
SEED_FILE = os.path.join(DATA, "seed_isins.txt")
OUT_FILE = os.path.join(DATA, "els.json")
STATUS_FILE = os.path.join(DATA, "status.json")

session = requests.Session()
session.headers.update(HEADERS)

LOG = []
def log(msg):
    line = f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)

# ---------------------------------------------------------------------------
# ISIN helpers
# ---------------------------------------------------------------------------
B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

def isin_check(isin11):
    s = "".join(str(ord(c) - 55) if c.isalpha() else c for c in isin11)
    digits = [int(d) for d in s][::-1]
    tot = 0
    for i, d in enumerate(digits):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        tot += d
    return str((10 - tot % 10) % 10)

def seq_to_isin(seq):
    s = ""
    n = seq
    for _ in range(3):
        s = B36[n % 36] + s
        n //= 36
    body = "KR6MD000" + s
    return body + isin_check(body)

def isin_to_seq(isin):
    try:
        return int(isin[8:11], 36)
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Detail page fetch + parse
# ---------------------------------------------------------------------------
def fetch_html(url, **kw):
    r = session.get(url, timeout=25, **kw)
    r.raise_for_status()
    enc = r.encoding or "euc-kr"
    if "euc" in enc.lower() or "ks_c" in enc.lower() or enc.lower() == "iso-8859-1":
        try:
            return r.content.decode("cp949", errors="replace")
        except Exception:
            pass
    return r.text

KDATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")

def kdate(s):
    m = KDATE.search(s or "")
    if not m:
        return None
    return "%04d-%02d-%02d" % tuple(int(x) for x in m.groups())

def parse_detail(html, isin):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)

    if "상품상세" not in text and "기초자산" not in text:
        return None
    m = re.search(r"미래에셋(?:증권|대우)\s*\(\s*(ELS|ELB|DLS|DLB)\s*\)\s*(\d{3,6})", text)
    if not m:
        return None
    ptype, code = m.group(1), m.group(2)

    def after(label, maxlen=400):
        i = text.find(label)
        if i < 0:
            return ""
        return text[i + len(label): i + len(label) + maxlen]

    # coupon
    coupon = None
    mm = re.search(r"연\s*수익률\s*\(세전\)\s*\n?\s*([\d.]+)\s*%", text)
    if mm:
        coupon = float(mm.group(1))
    else:
        mm = re.search(r"세전 수익률\s*:\s*연\s*([\d.]+)\s*%", text)
        if mm:
            coupon = float(mm.group(1))

    # ladder e.g. 90-90-85-85-80-75
    ladder = None
    i0 = text.find("상환조건")
    if i0 >= 0:
        seg = text[i0: i0 + 160]
        mm = re.search(r"(\d{2,3}(?:\.\d)?(?:\s*-\s*\d{2,3}(?:\.\d)?){1,12})", seg)
        if mm:
            ladder = [float(x) for x in re.split(r"\s*-\s*", mm.group(1).strip())]
            ladder = [int(x) if x == int(x) else x for x in ladder]

    # maturity / period
    mat_years = period_months = None
    mm = re.search(r"만기/\s*상환주기\s*\n?\s*([\d.]+)\s*(년|개월)\s*/\s*([\d.]+)\s*개월", text)
    if mm:
        v = float(mm.group(1))
        mat_years = v if mm.group(2) == "년" else v / 12.0
        period_months = float(mm.group(3))

    # dates
    issue_date = kdate(after("발행일", 60))
    ref_block = after("최초기준", 120)
    ref_date = kdate(ref_block)
    eval_block = after("자동조기상환", 900)
    evals = []
    for n, y, mo, d in re.findall(r"(\d{1,2})\s*차\s*:\s*(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", eval_block):
        evals.append((int(n), "%04d-%02d-%02d" % (int(y), int(mo), int(d))))
    evals = sorted(set(evals))
    mat_eval = kdate(after("만기상환평가일", 120))
    mat_pay = kdate(after("만기상환금 지급일", 80))

    # knock-in per asset
    ki = {}
    ki_block = after("낙인조건", 500)
    ki_block = ki_block.split("낙아웃")[0]
    for name, pct in re.findall(r"-\s*([^\n:]+?)\s*:\s*([\d.]+)\s*%", ki_block):
        ki[name.strip()] = float(pct)
    no_ki = False
    if not ki:
        first = [l.strip() for l in ki_block.split("\n") if l.strip()]
        first = [l for l in first if "Knock" not in l and "낙인" not in l]
        if first and first[0].upper().startswith("N"):
            no_ki = True

    # assets
    assets = list(ki.keys())
    if not assets:
        fb = after("1. 기초자산 :", 120).split("\n")[0]
        if fb.strip():
            assets = [a.strip() for a in fb.split("-") if a.strip()]
    if not assets:
        blk = after("기초자산", 200)
        blk = blk.split("상환조건")[0]
        assets = [l.strip() for l in blk.split("\n") if l.strip() and len(l.strip()) < 40][:5]

    # payoff text
    payoff = ""
    i = text.find("수익구조")
    if i >= 0:
        j = text.find("중도상환", i)
        payoff = text[i:j if j > 0 else i + 2500].strip()
    lizard = "리자드" in payoff or "Lizard" in payoff or "리자드" in text[:3000]
    lizard_rules = []
    if lizard:
        for n, pct in re.findall(r"(\d)\s*번째\s*자동조기상환평가일까지[^%\n]{0,80}?(\d{2,3}(?:\.\d)?)\s*%\s*미만[^\n]{0,60}?하락한\s*적이\s*없", payoff):
            lizard_rules.append([int(n), float(pct)])
        lizard_rules = sorted(set(map(tuple, lizard_rules)))
        lizard_rules = [list(x) for x in lizard_rules]
    monthly = "월지급" in text[:3000] or "월수익" in payoff
    principal_guard = "원금비보장" not in text[:2500]

    # coupon override from payoff (연 X%) if header missing
    if coupon is None:
        mm = re.search(r"연\s*([\d.]+)\s*%", payoff)
        if mm:
            coupon = float(mm.group(1))

    return {
        "isin": isin,
        "seq": isin_to_seq(isin),
        "type": ptype,
        "code": code,
        "name": f"미래에셋증권({ptype}){code}",
        "coupon": coupon,
        "ladder": ladder,
        "mat_years": mat_years,
        "period_months": period_months,
        "issue_date": issue_date,
        "ref_date": ref_date,
        "evals": evals,
        "mat_eval": mat_eval,
        "mat_pay": mat_pay,
        "ki": ki,
        "no_ki": no_ki,
        "assets": assets,
        "lizard": lizard,
        "lizard_rules": lizard_rules,
        "monthly": monthly,
        "principal_guard": principal_guard,
        "payoff": payoff[:1800],
        "pdf": f"{BASE}/public/hks4412/002/{isin}.pdf",
        "url": DETAIL_URL.format(isin=isin),
        "fetched": TODAY.isoformat(),
    }

HTTP_ERR_STREAK = [0]

def probe_isin(isin):
    """returns dict / None (not a product) / 'ERR' (network outage)"""
    try:
        html = fetch_html(DETAIL_URL.format(isin=isin))
        HTTP_ERR_STREAK[0] = 0
    except requests.HTTPError as e:
        # 4xx/5xx for a non-existent code is treated as "no product",
        # but a long streak means the site is down -> stop.
        HTTP_ERR_STREAK[0] += 1
        if HTTP_ERR_STREAK[0] > 30:
            return "ERR"
        return None
    except Exception:
        return "ERR"
    try:
        return parse_detail(html, isin)
    except Exception:
        log(f"parse error {isin}: {traceback.format_exc().splitlines()[-1]}")
        return None

# ---------------------------------------------------------------------------
# Universe discovery
# ---------------------------------------------------------------------------
ISIN_RE = re.compile(r"KR6MD0[0-9A-Z]{6}")

def try_list_endpoints():
    """Best-effort: read list pages + their JS, try candidate .do endpoints, harvest ISINs."""
    found = set()
    cands = set()
    for page in LIST_PAGES:
        try:
            html = fetch_html(page)
        except Exception as e:
            log(f"list page fail {page}: {e}")
            continue
        found |= set(ISIN_RE.findall(html))
        for u in re.findall(r"['\"](/hks/hks40[0-9]{2}/[A-Za-z0-9_]+\.do)['\"]", html):
            cands.add(u)
        for js in re.findall(r"<script[^>]+src=['\"]([^'\"]+\.js[^'\"]*)['\"]", html, flags=re.I):
            if js.startswith("//"):
                js = "https:" + js
            elif js.startswith("/"):
                js = BASE + js
            if "miraeasset" not in js:
                continue
            try:
                jt = session.get(js, timeout=20).text
                for u in re.findall(r"['\"](/hks/hks40[0-9]{2}/[A-Za-z0-9_]+\.do)['\"]", jt):
                    cands.add(u)
            except Exception:
                pass
    cands = sorted(c for c in cands if not c.endswith(("n01.do", "n02.do", "n02_2.do", "p02.do", "r01.do")))
    log(f"list endpoint candidates: {cands[:20]}")
    payload_variants = [
        {}, {"type": "ELS"}, {"prod_tp": "ELS"}, {"elsdls_tp": "ELS"},
        {"srch_gb": "2"}, {"gubun": "2"}, {"pageIndex": "1", "pageSize": "500"},
        {"page": "1", "pageSize": "500", "prod_tp": "ELS"},
    ]
    for c in cands[:12]:
        for pv in payload_variants:
            for method in ("post", "get"):
                try:
                    r = getattr(session, method)(BASE + c, data=pv if method == "post" else None,
                                                 params=pv if method == "get" else None, timeout=20)
                    body = r.content.decode("cp949", errors="replace")
                    hits = set(ISIN_RE.findall(body))
                    if hits:
                        log(f"endpoint {method.upper()} {c} {pv} -> {len(hits)} ISINs")
                        found |= hits
                        break
                except Exception:
                    pass
                time.sleep(0.15)
            if found:
                break
    return found

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)

def discover(products, scan, excluded=None):
    excluded = excluded or {}
    probes = 0
    new = 0
    excluded_seqs = set(isin_to_seq(i) for i in excluded.keys())
    # 1) seed file
    seeds = set()
    if os.path.exists(SEED_FILE):
        with open(SEED_FILE, encoding="utf-8") as f:
            for line in f:
                for i in ISIN_RE.findall(line.upper()):
                    seeds.add(i)
    # 2) list endpoints
    try:
        seeds |= try_list_endpoints()
    except Exception as e:
        log(f"list discovery error: {e}")
    for isin in sorted(seeds):
        if isin in products or isin in scan.get("empty", {}) or isin in excluded:
            continue
        if probes >= MAX_PROBES:
            break
        res = probe_isin(isin); probes += 1; time.sleep(REQ_SLEEP)
        if isinstance(res, dict):
            products[isin] = res; new += 1
        elif res is None:
            scan.setdefault("empty", {})[isin] = TODAY.isoformat()

    # 3) recent-first scan: estimate today's sequence number from known issue dates
    #    and probe downward from there, so newly issued products are found first.
    known_seqs = [p["seq"] for p in products.values() if p.get("seq")]
    pairs = sorted((date.fromisoformat(p["issue_date"]), p["seq"]) for p in products.values()
                   if p.get("seq") and p.get("issue_date"))
    rate = 2.8  # seq per calendar day (fallback)
    if len(pairs) >= 2 and (pairs[-1][0] - pairs[0][0]).days >= 45:
        rate = max(1.0, (pairs[-1][1] - pairs[0][1]) / (pairs[-1][0] - pairs[0][0]).days)
    if pairs:
        est_now = int(pairs[-1][1] + rate * ((TODAY - pairs[-1][0]).days + 14))
    else:
        est_now = int(SCAN_START_SEQ + rate * (TODAY - date(2023, 8, 1)).days) + 40
    recent_budget = int(os.environ.get("RECENT_PROBES", str(MAX_PROBES // 2 if FULL_SCAN else 120)))
    if not pairs:
        recent_budget = 0  # nothing to extrapolate from yet: let the forward scan build the base first
    empty_seqs_set = set(isin_to_seq(i) for i in scan.get("empty", {}).keys())
    checked_all = set(known_seqs) | empty_seqs_set | excluded_seqs
    max_known = max(known_seqs) if known_seqs else 0
    s = est_now
    rp = 0
    log(f"recent scan: rate={rate:.2f}/day est_now={est_now} max_known={max_known}")
    while rp < recent_budget and probes < MAX_PROBES and s > max(max_known, SCAN_START_SEQ):
        if s in checked_all:
            s -= 1; continue
        isin = seq_to_isin(s)
        res = probe_isin(isin); probes += 1; rp += 1; time.sleep(REQ_SLEEP)
        if isinstance(res, dict):
            products[isin] = res; new += 1
        elif res is None:
            scan.setdefault("empty", {})[isin] = TODAY.isoformat()
        else:
            log("network error in recent scan, stopping"); break
        s -= 1

    # 4) forward sequence scan from cursor (fills the historical range)
    known_seqs = [p["seq"] for p in products.values() if p.get("seq")]
    empty_seqs = [isin_to_seq(i) for i in scan.get("empty", {}).keys()]
    empty_seqs = [s for s in empty_seqs if s]
    max_known = max(known_seqs) if known_seqs else None
    cursor = scan.get("cursor")
    if cursor is None:
        cursor = SCAN_START_SEQ
    # forward frontier: continue from the historical cursor (recent end is covered by the recent scan)
    frontier = max(cursor, SCAN_START_SEQ)
    consecutive_miss = 0
    checked = set(known_seqs) | set(empty_seqs) | excluded_seqs
    s = frontier
    while probes < MAX_PROBES and consecutive_miss < FORWARD_MISS_LIMIT:
        if s in checked:
            s += 1
            continue
        isin = seq_to_isin(s)
        res = probe_isin(isin); probes += 1; time.sleep(REQ_SLEEP)
        if isinstance(res, dict):
            products[isin] = res; new += 1; consecutive_miss = 0
            scan["cursor"] = s + 1
        elif res is None:
            scan.setdefault("empty", {})[isin] = TODAY.isoformat()
            consecutive_miss += 1
            scan["cursor"] = s + 1
        else:  # network error: stop scanning to avoid marking as empty
            log(f"network error at seq {s}, pausing scan"); break
        s += 1
    # 5) back-fill holes below frontier when budget remains (full scan)
    if FULL_SCAN and probes < MAX_PROBES:
        for s2 in range(SCAN_START_SEQ, frontier):
            if probes >= MAX_PROBES:
                break
            if s2 in checked:
                continue
            isin = seq_to_isin(s2)
            res = probe_isin(isin); probes += 1; time.sleep(REQ_SLEEP)
            if isinstance(res, dict):
                products[isin] = res; new += 1
            elif res is None:
                scan.setdefault("empty", {})[isin] = TODAY.isoformat()
            else:
                break
    log(f"discovery: probes={probes} new={new} total_products={len(products)} cursor={scan.get('cursor')}")
    return probes, new

# ---------------------------------------------------------------------------
# Underlying price mapping
# ---------------------------------------------------------------------------
SYMBOLS = {
    # indices
    "KOSPI200": ("^KS200", "KOSPI200", "kr_index"),
    "KOSPI 200": ("^KS200", "KOSPI200", "kr_index"),
    "NIKKEI225": ("^N225", "Nikkei225", "jp"),
    "NIKKEI 225": ("^N225", "Nikkei225", "jp"),
    "EUROSTOXX50": ("^STOXX50E", "EuroStoxx50", "eu"),
    "EURO STOXX 50": ("^STOXX50E", "EuroStoxx50", "eu"),
    "EUROSTOXX 50": ("^STOXX50E", "EuroStoxx50", "eu"),
    "S&P500": ("^GSPC", "S&P500", "us"),
    "S&P 500": ("^GSPC", "S&P500", "us"),
    "HSCEI": ("^HSCE", "HSCEI", "hk"),
    "HSI": ("^HSI", "HSI", "hk"),
    "NASDAQ100": ("^NDX", "NASDAQ100", "us"),
    "DAX": ("^GDAXI", "DAX", "eu"),
    # KR stocks
    "삼성전자": ("005930.KS", "삼성전자", "kr"),
    "SK하이닉스": ("000660.KS", "SK하이닉스", "kr"),
    "현대차": ("005380.KS", "현대차", "kr"),
    "기아": ("000270.KS", "기아", "kr"),
    "NAVER": ("035420.KS", "NAVER", "kr"),
    "LG에너지솔루션": ("373220.KS", "LG에너지솔루션", "kr"),
    "삼성SDI": ("006400.KS", "삼성SDI", "kr"),
    "LG화학": ("051910.KS", "LG화학", "kr"),
    "카카오": ("035720.KS", "카카오", "kr"),
    "POSCO홀딩스": ("005490.KS", "POSCO홀딩스", "kr"),
    "KB금융": ("105560.KS", "KB금융", "kr"),
    "신한지주": ("055550.KS", "신한지주", "kr"),
    "한화에어로스페이스": ("012450.KS", "한화에어로스페이스", "kr"),
    # US stocks
    "TESLA": ("TSLA", "Tesla", "us"), "테슬라": ("TSLA", "Tesla", "us"), "TSLA": ("TSLA", "Tesla", "us"),
    "PALANTIR": ("PLTR", "Palantir", "us"), "팔란티어": ("PLTR", "Palantir", "us"), "PLTR": ("PLTR", "Palantir", "us"),
    "MICRON": ("MU", "Micron", "us"), "마이크론": ("MU", "Micron", "us"), "MU": ("MU", "Micron", "us"),
    "AMD": ("AMD", "AMD", "us"), "ADVANCED MICRO DEVICES": ("AMD", "AMD", "us"),
    "NVIDIA": ("NVDA", "NVIDIA", "us"), "엔비디아": ("NVDA", "NVIDIA", "us"), "NVDA": ("NVDA", "NVIDIA", "us"),
    "APPLE": ("AAPL", "Apple", "us"), "애플": ("AAPL", "Apple", "us"),
    "AMAZON": ("AMZN", "Amazon", "us"), "아마존": ("AMZN", "Amazon", "us"),
    "MICROSOFT": ("MSFT", "Microsoft", "us"), "마이크로소프트": ("MSFT", "Microsoft", "us"),
    "ALPHABET": ("GOOGL", "Alphabet", "us"), "GOOGLE": ("GOOGL", "Alphabet", "us"), "알파벳": ("GOOGL", "Alphabet", "us"),
    "META": ("META", "Meta", "us"), "메타": ("META", "Meta", "us"),
    "NETFLIX": ("NFLX", "Netflix", "us"), "넷플릭스": ("NFLX", "Netflix", "us"),
    "BROADCOM": ("AVGO", "Broadcom", "us"), "브로드컴": ("AVGO", "Broadcom", "us"),
    "COINBASE": ("COIN", "Coinbase", "us"), "코인베이스": ("COIN", "Coinbase", "us"),
    "INTEL": ("INTC", "Intel", "us"), "인텔": ("INTC", "Intel", "us"),
    "QUALCOMM": ("QCOM", "Qualcomm", "us"),
    "TSMC": ("TSM", "TSMC", "us"),
    "ARM": ("ARM", "Arm", "us"),
    "SUPER MICRO": ("SMCI", "Super Micro", "us"),
    "ELI LILLY": ("LLY", "Eli Lilly", "us"),
    "NOVO NORDISK": ("NVO", "Novo Nordisk", "us"),
    "ASML": ("ASML", "ASML", "us"),
    "BERKSHIRE": ("BRK-B", "Berkshire", "us"),
    "JPMORGAN": ("JPM", "JPMorgan", "us"),
    "EXXON": ("XOM", "Exxon", "us"),
    "BOEING": ("BA", "Boeing", "us"),
    "STARBUCKS": ("SBUX", "Starbucks", "us"),
    "DISNEY": ("DIS", "Disney", "us"),
    "UBER": ("UBER", "Uber", "us"),
    "PAYPAL": ("PYPL", "PayPal", "us"),
    "MICROSTRATEGY": ("MSTR", "Strategy", "us"), "STRATEGY": ("MSTR", "Strategy", "us"),
    "ALIBABA": ("BABA", "Alibaba", "us"),
}

def norm_name(n):
    return re.sub(r"[\s_\-\.]", "", n.upper())

NORM_MAP = {norm_name(k): v for k, v in SYMBOLS.items()}
NORM_KEYS = sorted(NORM_MAP.keys(), key=len, reverse=True)

def map_asset(name):
    n = norm_name(name)
    if n in NORM_MAP:
        return NORM_MAP[n]
    for k in NORM_KEYS:
        if len(k) >= 4 and len(n) >= 4 and (k in n or n in k):
            return NORM_MAP[k]
    return None

# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
PRICE_CACHE = {}

def yahoo_history(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(sym)}"
    params = {"range": "7y", "interval": "1d", "events": "div,split"}
    hdr = {"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"}
    for host in ("query1", "query2"):
        try:
            r = requests.get(url.replace("query1", host), params=params, headers=hdr, timeout=25)
            j = r.json()
            res = j["chart"]["result"][0]
            ts = res["timestamp"]
            closes = res["indicators"]["quote"][0]["close"]
            tzname = res["meta"].get("exchangeTimezoneName", "UTC")
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(tzname)
            except Exception:
                tz = timezone.utc
            out = {}
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                d = datetime.fromtimestamp(t, tz).date().isoformat()
                out[d] = float(c)
            if out:
                return out
        except Exception as e:
            log(f"yahoo fail {sym}@{host}: {e}")
    return None

def naver_history(code, is_index=False, pages=40):
    """Fallback for KR: m.stock.naver.com mobile API (closes by date)."""
    out = {}
    base = ("https://m.stock.naver.com/api/index/%s/price" % ("KPI200" if code == "KPI200" else code)) if is_index \
        else ("https://m.stock.naver.com/api/stock/%s/price" % code)
    hdr = {"User-Agent": HEADERS["User-Agent"], "Accept": "application/json", "Referer": "https://m.stock.naver.com/"}
    for p in range(1, pages + 1):
        try:
            r = requests.get(base, params={"pageSize": 60, "page": p}, headers=hdr, timeout=20)
            arr = r.json()
            if not isinstance(arr, list) or not arr:
                break
            for it in arr:
                d = str(it.get("localTradedAt", ""))[:10]
                c = str(it.get("closePrice", "")).replace(",", "")
                if d and c:
                    out[d] = float(c)
            time.sleep(0.15)
        except Exception as e:
            log(f"naver fail {code} p{p}: {e}")
            break
    return out or None

def naver_fchart(code, count=2000):
    """Fallback for KR: classic fchart XML (symbol=KPI200 for KOSPI200, or 6-digit stock code)."""
    url = "https://fchart.stock.naver.com/sise.nhn"
    hdr = {"User-Agent": HEADERS["User-Agent"], "Referer": "https://finance.naver.com/"}
    try:
        r = requests.get(url, params={"symbol": code, "timeframe": "day", "count": count, "requestType": "0"},
                         headers=hdr, timeout=25)
        out = {}
        for row in re.findall(r'data="([^"]+)"', r.text):
            parts = row.split("|")
            if len(parts) >= 5 and len(parts[0]) == 8:
                d = f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:]}"
                try:
                    out[d] = float(parts[4])
                except ValueError:
                    pass
        if out:
            log(f"fchart ok {code}: {len(out)} rows, last {max(out)}")
        return out or None
    except Exception as e:
        log(f"fchart fail {code}: {e}")
        return None

NOW_KST = datetime.now(KST)

def drop_open_bar(h, market):
    """Remove a bar that may still be intraday when the job runs during market hours."""
    if not h:
        return h
    today = TODAY.isoformat()
    if today in h:
        if market in ("kr", "kr_index", "jp") and NOW_KST.hour < 16:
            h = {k: v for k, v in h.items() if k != today}
        elif market in ("us", "eu"):
            h = {k: v for k, v in h.items() if k != today}
    return h

def get_history(sym, market):
    if sym in PRICE_CACHE:
        return PRICE_CACHE[sym]
    h = yahoo_history(sym)
    if h and market in ("kr", "kr_index"):
        # Yahoo KR data is sometimes stale/gappy: distrust if last bar older than 7 days
        if (TODAY - date.fromisoformat(max(h))).days > 7:
            log(f"yahoo {sym} stale (last {max(h)}), trying Naver")
            h = None
    if (not h) and market in ("kr", "kr_index"):
        code = "KPI200" if sym == "^KS200" else sym.split(".")[0]
        h = naver_fchart(code) or naver_history(code, is_index=(sym == "^KS200"))
    h = drop_open_bar(h or {}, market)
    PRICE_CACHE[sym] = h
    return PRICE_CACHE[sym]

def close_on(hist, d, forward=False, max_days=6):
    """close on date d; if missing, previous (default) or next trading day within max_days."""
    if d in hist:
        return hist[d], d
    dd = date.fromisoformat(d)
    for k in range(1, max_days + 1):
        x = (dd + timedelta(days=k)) if forward else (dd - timedelta(days=k))
        xs = x.isoformat()
        if xs in hist:
            return hist[xs], xs
    return None, None

# ---------------------------------------------------------------------------
# Judgment
# ---------------------------------------------------------------------------
def build_schedule(p):
    """list of evaluations: [{n, date, barrier, is_maturity, months}]"""
    ladder = p.get("ladder") or []
    evals = list(p.get("evals") or [])
    sched = []
    dates = [d for _, d in evals]
    me = p.get("mat_eval")
    if me and (not dates or dates[-1] != me):
        dates.append(me)
    if not dates:
        return sched
    # align barriers: if ladder shorter/longer than dates, pad with last barrier
    for i, d in enumerate(dates):
        if ladder:
            b = ladder[i] if i < len(ladder) else ladder[-1]
        else:
            b = None
        months = None
        if p.get("issue_date"):
            d0 = date.fromisoformat(p["issue_date"]); d1 = date.fromisoformat(d)
            months = round((d1 - d0).days / 30.4375)
        sched.append({"n": i + 1, "date": d, "barrier": b,
                      "is_maturity": (i == len(dates) - 1) and bool(me),
                      "months": months})
    return sched

def judge(p, red=None):
    out = {"isin": p["isin"], "code": p["code"], "type": p["type"], "name": p["name"],
           "coupon": p.get("coupon"), "ladder": p.get("ladder"), "issue_date": p.get("issue_date"),
           "ref_date": p.get("ref_date"), "mat_eval": p.get("mat_eval"), "mat_pay": p.get("mat_pay"),
           "mat_years": p.get("mat_years"), "period_months": p.get("period_months"),
           "lizard": p.get("lizard"), "monthly": p.get("monthly"), "principal_guard": p.get("principal_guard"),
           "no_ki": p.get("no_ki"), "pdf": p.get("pdf"), "url": p.get("url"),
           "payoff": p.get("payoff", "")[:1200], "assets": [], "schedule": build_schedule(p),
           "flags": []}
    if red:
        out["assets"] = [{"name": n, "label": (map_asset(n) or (None, n))[1]} for n in (p.get("assets") or [])]
        out["judgment"] = {"status": "redeemed_confirmed", "worst": None, "next": None, "expected": None,
                           "knocked_in": None, "need_pct": None, "ki_room": None,
                           "redeemed": {"date": red.get("date"), "kind": red.get("kind"), "source": red.get("source")},
                           "message": f"예탁결제원 상환종목 공시 확인 — {red.get('kind') or '상환'} {red.get('date') or ''}".strip()}
        return out
    ref_date = p.get("ref_date") or p.get("issue_date")
    assets_out = []
    unsupported = []
    for name in p.get("assets") or []:
        m = map_asset(name)
        a = {"name": name, "sym": None, "label": name, "ref": None, "ref_date": None,
             "last": None, "last_date": None, "ratio": None, "ki": p.get("ki", {}).get(name),
             "min_ratio": None, "min_date": None}
        if not m or not ref_date:
            unsupported.append(name); assets_out.append(a); continue
        sym, label, market = m
        a["sym"], a["label"] = sym, label
        hist = get_history(sym, market)
        if not hist:
            unsupported.append(name); assets_out.append(a); continue
        ref, rd = close_on(hist, ref_date, forward=True)
        if ref is None:
            unsupported.append(name); assets_out.append(a); continue
        last_date = max(hist.keys())
        last = hist[last_date]
        a.update({"ref": ref, "ref_date": rd, "last": last, "last_date": last_date, "ratio": last / ref * 100})
        if rd != ref_date:
            out["flags"].append(f"{label}: 최초기준가격결정일({ref_date}) 종가 없음 → {rd} 종가 사용")
        # min close since reference date
        mn, md = None, None
        for d, c in hist.items():
            if d > rd:
                if mn is None or c < mn:
                    mn, md = c, d
        if mn is not None:
            a["min_ratio"] = mn / ref * 100
            a["min_date"] = md
        # closes at each evaluation date (own exchange calendar)
        a["eval_ratios"] = {}
        for ev in out["schedule"]:
            if ev["date"] <= last_date:
                c, cd = close_on(hist, ev["date"], forward=False, max_days=4)
                if c is not None:
                    a["eval_ratios"][ev["date"]] = round(c / ref * 100, 2)
        assets_out.append(a)
    out["assets"] = assets_out
    if unsupported:
        out["flags"].append("가격 데이터 미지원 기초자산: " + ", ".join(unsupported))

    valid = [a for a in assets_out if a.get("ratio") is not None]
    sched = out["schedule"]
    j = {"status": "unknown", "message": "", "worst": None, "next": None, "expected": None,
         "knocked_in": None, "redeemed": None, "need_pct": None, "ki_room": None}
    if not valid or len(valid) != len(assets_out) or not sched:
        j["status"] = "nodata"
        j["message"] = "기초자산 가격 또는 상환일정 데이터가 부족하여 자동 판정하지 못했습니다. 상품설명서를 확인해 주세요."
        out["judgment"] = j
        return out

    worst = min(valid, key=lambda a: a["ratio"])
    j["worst"] = {"label": worst["label"], "ratio": round(worst["ratio"], 2)}
    # knock-in
    ki_levels = [a.get("ki") for a in valid if a.get("ki")]
    knocked = False
    if ki_levels:
        for a in valid:
            if a.get("ki") and a.get("min_ratio") is not None and a["min_ratio"] < a["ki"]:
                knocked = True
        j["knocked_in"] = knocked
        j["ki_room"] = round(min((a["ratio"] - a["ki"]) for a in valid if a.get("ki")), 2)
    else:
        j["knocked_in"] = None

    # lizard helper: min ratio of every asset from reference date up to a given date
    lz_rules = {int(n): float(pct) for n, pct in (p.get("lizard_rules") or [])}
    for ev in sched:
        if ev["n"] in lz_rules:
            ev["lizard"] = lz_rules[ev["n"]]

    # past evaluations: redeemed?
    last_dates = [a["last_date"] for a in valid]
    redeemed_at = None
    lizard_hit = False
    for ev in sched:
        if ev["date"] <= min(last_dates) and ev["barrier"] is not None:
            rs = [a.get("eval_ratios", {}).get(ev["date"]) for a in valid]
            if all(r is not None for r in rs) and all(r >= ev["barrier"] for r in rs):
                redeemed_at = ev
                break
            if ev["n"] in lz_rules:
                pct = lz_rules[ev["n"]]
                ok = True
                for a in valid:
                    hist = PRICE_CACHE.get(a["sym"]) or {}
                    mins = [c for d, c in hist.items() if a["ref_date"] < d <= ev["date"]]
                    if not mins or min(mins) / a["ref"] * 100 < pct:
                        ok = False; break
                if ok:
                    redeemed_at = ev; lizard_hit = True
                    break
            ev["result"] = "missed" if all(r is not None for r in rs) else "unknown"
    if redeemed_at:
        j["redeemed"] = {"n": redeemed_at["n"], "date": redeemed_at["date"], "is_maturity": redeemed_at["is_maturity"],
                         "months": redeemed_at["months"]}
        for ev in sched:
            if ev["n"] == redeemed_at["n"]:
                ev["result"] = "passed"
        ret = None
        if p.get("coupon") and redeemed_at["months"]:
            ret = round(p["coupon"] * redeemed_at["months"] / 12.0, 2)
        j["status"] = "redeemed"
        if lizard_hit:
            j["message"] = f"{redeemed_at['n']}차 평가일({redeemed_at['date']}) 리자드 조건(평가일까지 {lz_rules[redeemed_at['n']]}% 미만 하락 없음) 충족 → 상환 완료로 추정" + (f" (세전 약 {ret}%)" if ret else "")
        else:
            j["message"] = f"{redeemed_at['n']}차 평가일({redeemed_at['date']})에 모든 기초자산이 배리어 {redeemed_at['barrier']}% 이상 → 상환 완료로 추정" + (f" (세전 약 {ret}%)" if ret else "")
        j["expected"] = {"n": redeemed_at["n"], "date": redeemed_at["date"], "months": redeemed_at["months"],
                         "barrier": redeemed_at["barrier"], "ret": ret}
        out["judgment"] = j
        return out

    upcoming = [ev for ev in sched if ev["date"] > min(last_dates)]
    if not upcoming:
        j["status"] = "matured"
        j["message"] = "만기 평가일이 지났습니다. 실제 상환 결과는 발행사 안내를 확인해 주세요."
        out["judgment"] = j
        return out
    nxt = upcoming[0]
    j["next"] = {"n": nxt["n"], "date": nxt["date"], "barrier": nxt["barrier"], "is_maturity": nxt["is_maturity"],
                 "months": nxt["months"], "days": (date.fromisoformat(nxt["date"]) - TODAY).days}
    if nxt["barrier"] is None:
        j["status"] = "nodata"; j["message"] = "상환조건(배리어)을 읽지 못했습니다."
        out["judgment"] = j; return out
    wr = worst["ratio"]
    need = max(0.0, nxt["barrier"] / wr * 100 - 100)
    j["need_pct"] = round(need, 2)
    coupon = p.get("coupon") or 0

    def ret_at(ev):
        return round(coupon * ev["months"] / 12.0, 2) if ev.get("months") else None

    if wr >= nxt["barrier"]:
        j["status"] = "pass"
        j["expected"] = {"n": nxt["n"], "date": nxt["date"], "months": nxt["months"], "barrier": nxt["barrier"], "ret": ret_at(nxt)}
        j["message"] = (f"현재 수준(최저 {worst['label']} {wr:.1f}%)이 {nxt['n']}차 배리어 {nxt['barrier']}% 이상 → "
                        f"{nxt['date']} {'만기' if nxt['is_maturity'] else '조기'}상환 가능 (세전 약 {ret_at(nxt)}%)")
    else:
        later = [ev for ev in upcoming[1:] if ev["barrier"] is not None and wr >= ev["barrier"]]
        if later:
            ev = later[0]
            j["status"] = "defer"
            j["expected"] = {"n": ev["n"], "date": ev["date"], "months": ev["months"], "barrier": ev["barrier"], "ret": ret_at(ev)}
            j["message"] = (f"{nxt['n']}차({nxt['date']}) 배리어 {nxt['barrier']}% 미달 (최저 {worst['label']} {wr:.1f}%, +{need:.1f}% 필요) → "
                            f"현 수준 유지 시 {ev['n']}차({ev['date']}) 배리어 {ev['barrier']}%에서 상환 예상 (세전 약 {ret_at(ev)}%)")
        else:
            if knocked:
                j["status"] = "ki"
                j["message"] = (f"낙인 발생 이력 있음 + 현 수준(최저 {worst['label']} {wr:.1f}%)이 만기 배리어 미달 → "
                                f"만기까지 회복하지 못하면 원금손실 구간. 만기 배리어까지 +{(sched[-1]['barrier']/wr*100-100):.1f}% 필요")
            elif ki_levels:
                j["status"] = "risk"
                j["message"] = (f"현 수준(최저 {worst['label']} {wr:.1f}%)으로는 만기 배리어까지 미달. 낙인 미발생 상태이므로 "
                                f"만기까지 낙인({min(ki_levels):.0f}%) 미터치 시 만기 수익 지급 가능 (낙인까지 여유 {j['ki_room']:.1f}%p)")
            else:
                j["status"] = "risk"
                j["message"] = f"현 수준(최저 {worst['label']} {wr:.1f}%)으로는 모든 배리어 미달 — 상품설명서의 만기 조건을 확인해 주세요."
    if knocked and j["status"] in ("pass", "defer"):
        j["message"] += " ※ 과거 낙인 터치 이력이 있으나 배리어 충족 시 정상 상환"
    # upcoming lizard chance
    for ev in upcoming:
        if ev["n"] in lz_rules:
            pct = lz_rules[ev["n"]]
            worst_min = min(a["min_ratio"] for a in valid if a.get("min_ratio") is not None) if any(a.get("min_ratio") is not None for a in valid) else None
            if worst_min is not None:
                if worst_min >= pct:
                    note = f"리자드: {ev['n']}차({ev['date']})까지 {pct}% 미만 하락이 없으면 리자드 상환 — 현재 기간 최저 {worst_min:.1f}%로 조건 유지 중"
                    if j["status"] in ("defer", "risk"):
                        j["message"] += " · " + note
                        if j["status"] == "defer" and (j.get("expected") or {}).get("n", 99) > ev["n"]:
                            j["expected"] = {"n": ev["n"], "date": ev["date"], "months": ev["months"], "barrier": pct, "ret": ret_at(ev), "lizard": True}
                    else:
                        out["flags"].append(note)
                else:
                    out["flags"].append(f"리자드: {ev['n']}차 리자드 배리어 {pct}% 아래로 이미 하락한 이력 있음(기간 최저 {worst_min:.1f}%) → 리자드 상환 불가")
            break
    if p.get("lizard") and not lz_rules:
        out["flags"].append("리자드 조건 상품 — 리자드 조건을 자동으로 읽지 못했으니 상품설명서로 별도 확인 필요")
    if p.get("monthly"):
        out["flags"].append("월지급식 상품 — 월 쿠폰 지급 조건은 별도")
    out["judgment"] = j
    return out

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    t0 = time.time()
    products = load_json(PRODUCTS_FILE, {})
    scan = load_json(SCAN_FILE, {"cursor": None, "empty": {}})
    redeemed = load_json(REDEEMED_FILE, {})
    excluded = load_json(EXCLUDED_FILE, {})
    log(f"start: cached products={len(products)} excluded={len(excluded)} FULL_SCAN={FULL_SCAN} MAX_PROBES={MAX_PROBES}")

    # 0) redemption list first (KSD SEIBro). Never re-read anything already excluded.
    red_ok = False
    if redeemed_mod and os.environ.get("SKIP_REDEEMED", "0") != "1":
        try:
            found, red_ok = redeemed_mod.fetch_redeemed(days_back=420, logf=log)
            for k, v in found.items():
                if k not in redeemed:
                    redeemed[k] = v
            save_json(REDEEMED_FILE, redeemed)
        except Exception:
            log("redeemed fetch crashed: " + traceback.format_exc().splitlines()[-1])
    else:
        log("redeemed cross-check skipped")

    probes = new = 0
    try:
        probes, new = discover(products, scan, excluded)
    except Exception:
        log("discovery crashed: " + traceback.format_exc().splitlines()[-1])
    save_json(SCAN_FILE, scan)

    def red_for(p):
        r = redeemed.get(p["isin"]) or redeemed.get("CODE:" + p["code"])
        return r

    # active universe: not excluded, ELS/ELB, not matured > 45 days ago
    active = []
    for isin, p in list(products.items()):
        if isin in excluded:
            continue
        if p.get("type") not in ("ELS", "ELB"):
            excluded[isin] = {"reason": "type:" + str(p.get("type")), "date": TODAY.isoformat()}
            continue
        me = p.get("mat_eval")
        if me and date.fromisoformat(me) < TODAY - timedelta(days=45):
            excluded[isin] = {"reason": "matured", "date": TODAY.isoformat()}
            continue
        active.append(p)
    log(f"active products: {len(active)} (redeemed list entries={len(redeemed)}, source ok={red_ok})")

    results = []
    for p in sorted(active, key=lambda x: int(x["code"])):
        try:
            results.append(judge(p, red_for(p)))
        except Exception:
            log(f"judge error {p.get('isin')}: {traceback.format_exc().splitlines()[-1]}")

    # exclusion bookkeeping:
    #  - confirmed by KSD list: keep visible (search only) for 30 days after redemption date, then exclude for good
    #  - computed (estimated) redemption: keep 45 days, then exclude for good
    kept = []
    hidden_est = 0
    for r in results:
        j = r.get("judgment", {})
        st = j.get("status")
        d = (j.get("redeemed") or {}).get("date")
        if st == "redeemed_confirmed":
            if d and date.fromisoformat(d) < TODAY - timedelta(days=30):
                excluded[r["isin"]] = {"reason": "redeemed:confirmed", "date": d}
                continue
        elif st == "redeemed":
            # estimated only (price-based): hide from the site after 45 days but keep in cache,
            # so a later KSD confirmation (or a data correction) can still act on it
            if d and date.fromisoformat(d) < TODAY - timedelta(days=45):
                hidden_est += 1
                continue
        kept.append(r)
    # shrink the product cache: excluded products are never read again
    for isin in list(products.keys()):
        if isin in excluded:
            products.pop(isin, None)
    save_json(PRODUCTS_FILE, products)
    save_json(EXCLUDED_FILE, excluded)

    price_asof = {}
    for sym, h in PRICE_CACHE.items():
        if h:
            d = max(h.keys()); price_asof[sym] = {"date": d, "close": h[d]}

    counts = {}
    unsupported = {}
    for r in kept:
        st_ = r["judgment"]["status"]; counts[st_] = counts.get(st_, 0) + 1
        if st_ == "nodata":
            for f in r.get("flags", []):
                if f.startswith("가격 데이터 미지원"):
                    for nm in f.split(":", 1)[1].split(","):
                        nm = nm.strip(); unsupported[nm] = unsupported.get(nm, 0) + 1
            if not r.get("schedule") or any(e.get("barrier") is None for e in r.get("schedule", [])):
                unsupported["(배리어 미해석)"] = unsupported.get("(배리어 미해석)", 0) + 1
    unsupported = dict(sorted(unsupported.items(), key=lambda x: -x[1])[:25])

    out = {"generated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "asof": TODAY.isoformat(),
           "prices": price_asof, "counts": counts, "products": kept}
    save_json(OUT_FILE, out)
    status = {"generated": out["generated"], "elapsed_sec": round(time.time() - t0, 1),
              "cached_products": len(products), "excluded": len(excluded), "redeemed_list": len(redeemed),
              "redeemed_source_ok": red_ok, "hidden_estimated": hidden_est, "active": len(kept), "probes": probes, "new": new,
              "counts": counts, "nodata_reasons": unsupported, "scan_cursor": scan.get("cursor"), "log": LOG[-120:]}
    save_json(STATUS_FILE, status)
    log(f"done in {status['elapsed_sec']}s — active={len(kept)} counts={counts}")

if __name__ == "__main__":
    main()
