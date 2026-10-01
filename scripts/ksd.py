# -*- coding: utf-8 -*-
"""
KSD open API (data.go.kr 한국예탁결제원_파생결합증권정보서비스_GW) client.

Operations used (all take serviceKey, pageNo, numOfRows, isin):
  getErlyRedELSInfo        - early-redemption date of an ELS (redemption cross-check)
  getDerivCombiIsinInfoN1  - basic info (issue date, maturity, name)  * public issues only
  getRedCondiInfoN1        - redemption conditions (evaluation start/end dates, pay dates, barrier)
  getAssetXrcInfoN1        - underlying exercise info (base ratio, base price) -> official 최초기준가
  getAssetInfoN1           - underlying asset names/codes

Daily traffic is limited (100 calls per operation on a development account), so a
per-day usage ledger is kept in data/ksd_usage.json and the caller prioritises.
Field names are not documented here, so the first raw record of each operation is
saved to data/ksd_debug.json and extraction uses tolerant key matching.
"""
import os, re, json, time
from datetime import date, datetime

import requests

KEY = os.environ.get("KSD_API_KEY", "").strip()
ENDPOINT = os.environ.get("KSD_ENDPOINT", "").strip().rstrip("/")
CANDIDATE_BASES = [
    "https://apis.data.go.kr/B552481/DerivesSvc",   # KSD 파생결합증권정보서비스_GW (confirmed End Point)
    "http://apis.data.go.kr/B552481/DerivesSvc",
]
DAILY_LIMIT = int(os.environ.get("KSD_DAILY_LIMIT", "95"))

_state = {"base": None, "usage": None, "usage_path": None, "debug": {}, "log": None}

def _log(msg):
    if _state["log"]:
        _state["log"]("[ksd] " + msg)

def setup(data_dir, logf=None):
    _state["log"] = logf
    _state["usage_path"] = os.path.join(data_dir, "ksd_usage.json")
    _state["debug_path"] = os.path.join(data_dir, "ksd_debug.json")
    try:
        with open(_state["usage_path"], encoding="utf-8") as f:
            _state["usage"] = json.load(f)
    except Exception:
        _state["usage"] = {}
    today = date.today().isoformat()
    if _state["usage"].get("date") != today:
        _state["usage"] = {"date": today, "ops": {}}
    if ENDPOINT:
        _state["base"] = ENDPOINT
    return bool(KEY)

def save():
    try:
        with open(_state["usage_path"], "w", encoding="utf-8") as f:
            json.dump(_state["usage"], f, ensure_ascii=False, indent=1)
        with open(_state["debug_path"], "w", encoding="utf-8") as f:
            json.dump(_state["debug"], f, ensure_ascii=False, indent=1)
    except Exception:
        pass

def remaining(op):
    used = _state["usage"]["ops"].get(op, 0)
    return max(0, DAILY_LIMIT - used)

def _count(op):
    _state["usage"]["ops"][op] = _state["usage"]["ops"].get(op, 0) + 1

def _items_from(payload):
    """Extract item list from the usual data.go.kr envelope (json)."""
    try:
        body = payload["response"]["body"]
        items = body.get("items")
        if items is None:
            return [], body
        if isinstance(items, dict):
            items = items.get("item", [])
        if isinstance(items, dict):
            items = [items]
        return items or [], body
    except Exception:
        return [], {}

def _xml_items(text):
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.fromstring(text)
        for it in root.iter("item"):
            out.append({c.tag: (c.text or "").strip() for c in it})
        hdr = {c.tag: (c.text or "") for c in root.iter("header") for c in c}
        return out, hdr
    except Exception:
        return [], {}

def call(op, isin=None, rows=100, page=1, extra=None):
    """Returns (items, ok). ok=False means quota/transport/auth problem (not 'no data')."""
    if not KEY:
        return [], False
    if remaining(op) <= 0:
        return [], False
    bases = [_state["base"]] if _state["base"] else CANDIDATE_BASES
    params = {"serviceKey": KEY, "pageNo": page, "numOfRows": rows, "resultType": "json"}
    if isin:
        params["isin"] = isin
    if extra:
        params.update(extra)
    for base in bases:
        url = f"{base}/{op}"
        try:
            r = requests.get(url, params=params, timeout=30)
            _count(op)
            txt = r.text
            items, hdr = [], {}
            if txt.lstrip().startswith("{"):
                try:
                    j = r.json()
                    items, body = _items_from(j)
                    hdr = j.get("response", {}).get("header", {})
                except Exception:
                    pass
            else:
                items, hdr = _xml_items(txt)
            code = str(hdr.get("resultCode", "")) if isinstance(hdr, dict) else ""
            if r.status_code == 200 and (items or code in ("00", "0", "03")):
                if not _state["base"]:
                    _state["base"] = base
                    _log(f"endpoint resolved: {base}")
                if items and op not in _state["debug"]:
                    _state["debug"][op] = {"sample_isin": isin, "first_item": items[0], "count": len(items)}
                return items, True
            if r.status_code == 200 and code and code not in ("00", "0"):
                msg = hdr.get("resultMsg", "") if isinstance(hdr, dict) else ""
                _log(f"{op} {isin or ''} resultCode={code} {msg}")
                if code in ("22", "30", "31", "32", "99"):  # quota / key problems
                    return [], False
                return [], True
            _log(f"{op} @{base.rsplit('/',1)[-1]} http={r.status_code} code={code or '-'} body={txt[:140]!r}")
            if _state["base"]:
                return [], False
        except Exception as e:
            _log(f"{op} error at {base}: {e}")
            if _state["base"]:
                return [], False
    if not _state["base"]:
        _log("no endpoint base worked; set KSD_ENDPOINT secret to the End Point URL shown on data.go.kr")
    return [], False

# ---------------------------------------------------------------------------
# tolerant field extraction
# ---------------------------------------------------------------------------
def _find(item, patterns):
    for k, v in item.items():
        kl = k.lower()
        for pat in patterns:
            if re.search(pat, kl):
                return v
    return None

def _dt(v):
    if v is None:
        return None
    s = re.sub(r"\D", "", str(v))
    if len(s) >= 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None

def _num(v):
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return None

def basic_info(isin):
    items, ok = call("getDerivCombiIsinInfoN1", isin, rows=10)
    if not items:
        return None, ok
    it = items[0]
    return {
        "name": _find(it, [r"nm$", r"name"]),
        "issue_date": _dt(_find(it, [r"issu.*dt", r"issue.*d"])),
        "maturity": _dt(_find(it, [r"xpir.*dt", r"expir", r"mtr.*dt", r"matur"])),
        "raw": it,
    }, ok

def red_conditions(isin):
    """Returns list of {n, eval_start, eval_end, pay_date, barrier, kind} sorted by date."""
    items, ok = call("getRedCondiInfoN1", isin, rows=100)
    if not items:
        return None, ok
    rows = []
    for it in items:
        st = _dt(_find(it, [r"mdeval.*strt", r"eval.*st.*dt", r"strt.*dt", r"begin.*dt", r"st.*dt"]))
        en = _dt(_find(it, [r"mdeval.*end", r"eval.*end.*dt", r"end.*dt"]))
        pay = _dt(_find(it, [r"pay.*dt", r"pymt.*dt"]))
        bar = _num(_find(it, [r"xrc.*rt", r"red.*rt", r"cond.*rt", r"barri", r"rt$"]))
        kind = _find(it, [r"red.*tp", r"tpnm", r"kind", r"gubun", r"type"])
        seq = _num(_find(it, [r"seq", r"ord", r"no$", r"tms"]))
        rows.append({"eval_start": st, "eval_end": en, "pay_date": pay, "barrier": bar, "kind": kind, "seq": seq, "raw": it})
    rows = [r for r in rows if r["eval_end"] or r["eval_start"]]
    rows.sort(key=lambda r: (r["eval_end"] or r["eval_start"]))
    for i, r in enumerate(rows):
        r["n"] = i + 1
    return rows, ok

def asset_exercise(isin):
    """Returns list of {name, code, base_ratio, base_price}."""
    items, ok = call("getAssetXrcInfoN1", isin, rows=50)
    if not items:
        return None, ok
    out = []
    for it in items:
        out.append({
            "name": _find(it, [r"asst.*nm", r"undr.*nm", r"nm$", r"name"]),
            "code": _find(it, [r"asst.*isin", r"asst.*cd", r"undr.*cd", r"isin$", r"cd$"]),
            "base_ratio": _num(_find(it, [r"xrc.*rt", r"base.*rt", r"rt$"])),
            "base_price": _num(_find(it, [r"xrc.*prc", r"base.*prc", r"prc$", r"price"])),
            "raw": it,
        })
    return out, ok

def early_redeemed(isin):
    """Returns (date or None, ok). None with ok=True means not redeemed (no record)."""
    items, ok = call("getErlyRedELSInfo", isin, rows=10)
    if not items:
        return None, ok
    it = items[0]
    d = _dt(_find(it, [r"red.*dt", r"erly.*dt", r"dt$", r"date"]))
    return (d or "unknown"), ok
