# -*- coding: utf-8 -*-
"""
Redemption cross-check for MIRAE-ELS-CHECK.

Source: KSD SEIBro (mobile) "ELS/DLS 상환종목" list.
The page is a classic form; field names may change, so this module discovers
the form (action + inputs + issuer select) at runtime and logs what it found.

Result: dict {isin_or_code: {"date": "YYYY-MM-DD", "kind": "조기상환|만기상환|...", "name": "...", "source": "seibro"}}
"""
import re, time, json
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

M_BASE = "https://m.seibro.or.kr"
RED_PATH = "/cnts/derivcombi/selectDerivELSDLSRedIssues.do"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-S918N) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Mobile Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": M_BASE + "/common/index.do",
}
ISIN_RE = re.compile(r"KR6MD0[0-9A-Z]{6}")
CODE_RE = re.compile(r"미래에셋(?:증권|대우)?\s*\(?\s*(ELS|ELB|DLS|DLB)?\s*\)?\s*제?\s*(\d{4,6})\s*회?")
DATE_RE = re.compile(r"(20\d{2})[.\-/년 ]\s*(\d{1,2})[.\-/월 ]\s*(\d{1,2})")

def _log(logf, msg):
    if logf:
        logf("[redeemed] " + msg)

def _dt(s):
    m = DATE_RE.search(s or "")
    if not m:
        return None
    return "%04d-%02d-%02d" % tuple(int(x) for x in m.groups())

def discover_form(sess, logf=None):
    r = sess.get(M_BASE + RED_PATH, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    forms = soup.find_all("form")
    info = {"action": RED_PATH, "fields": {}, "issuer_field": None, "issuer_value": None,
            "date_fields": [], "html_len": len(r.text)}
    for f in forms:
        act = f.get("action") or ""
        if "Red" in act or "derivcombi" in act or not info["fields"]:
            info["action"] = act or RED_PATH
            for inp in f.find_all(["input", "select"]):
                nm = inp.get("name")
                if not nm:
                    continue
                if inp.name == "select":
                    opts = inp.find_all("option")
                    vals = [(o.get("value", ""), o.get_text(strip=True)) for o in opts]
                    info["fields"][nm] = vals[0][0] if vals else ""
                    for v, t in vals:
                        if "미래에셋" in t:
                            info["issuer_field"], info["issuer_value"] = nm, v
                else:
                    info["fields"][nm] = inp.get("value", "")
                    low = nm.lower()
                    if any(k in low for k in ("dt", "date", "day", "ymd")):
                        info["date_fields"].append(nm)
    _log(logf, f"form action={info['action']} fields={list(info['fields'].keys())[:20]} "
               f"issuer={info['issuer_field']}={info['issuer_value']} date_fields={info['date_fields']}")
    return info, r.text

def parse_rows(html):
    """Extract (key, date, kind, name) from result tables/lists."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    blocks = soup.find_all("tr") + soup.find_all("li") + soup.find_all("dl")
    for b in blocks:
        txt = b.get_text(" ", strip=True)
        if "미래에셋" not in txt and "KR6MD" not in txt:
            continue
        key = None
        m = ISIN_RE.search(txt)
        if m:
            key = m.group(0)
        else:
            m = CODE_RE.search(txt)
            if m:
                key = "CODE:" + m.group(2)
        if not key:
            continue
        d = _dt(txt)
        kind = "조기상환" if "조기" in txt else ("만기상환" if "만기" in txt else ("중도상환" if "중도" in txt else "상환"))
        out[key] = {"date": d, "kind": kind, "name": txt[:80], "source": "seibro"}
    return out

def fetch_redeemed(days_back=420, logf=None, max_pages=60):
    """Returns (redeemed_dict, ok_flag). ok_flag False means the source could not be read."""
    sess = requests.Session()
    sess.headers.update(HEADERS)
    found = {}
    try:
        info, first_html = discover_form(sess, logf)
    except Exception as e:
        _log(logf, f"page fetch failed: {e}")
        return {}, False
    found.update(parse_rows(first_html))

    today = date.today()
    frm = today - timedelta(days=days_back)
    payload = dict(info["fields"])
    if info["issuer_field"]:
        payload[info["issuer_field"]] = info["issuer_value"]
    # date fields: assign from/to by order or by name hints
    dfs = info["date_fields"]
    for nm in dfs:
        low = nm.lower()
        val = frm if any(k in low for k in ("from", "start", "st", "bgn", "begin", "fr")) else today
        if "to" in low or "end" in low or "ed" in low:
            val = today
        payload[nm] = val.strftime("%Y%m%d")
    if len(dfs) == 2:
        payload[dfs[0]] = frm.strftime("%Y%m%d")
        payload[dfs[1]] = today.strftime("%Y%m%d")

    action = info["action"]
    url = action if action.startswith("http") else (M_BASE + (action if action.startswith("/") else "/" + action))
    ok = False
    for page in range(1, max_pages + 1):
        p = dict(payload)
        # common mobile paging keys on m.seibro
        for k in ("current", "page", "pageNo", "pageIndex"):
            if k in p or page > 1:
                p[k] = str(page)
        try:
            r = sess.post(url, data=p, timeout=25)
            html = r.text
        except Exception as e:
            _log(logf, f"post failed p{page}: {e}")
            break
        rows = parse_rows(html)
        if page == 1:
            _log(logf, f"page1 rows={len(rows)} html_len={len(html)}")
        if not rows:
            if page == 1:
                # try GET variant
                try:
                    r = sess.get(url, params=p, timeout=25)
                    rows = parse_rows(r.text)
                    _log(logf, f"GET variant rows={len(rows)}")
                except Exception:
                    pass
            if not rows:
                break
        ok = True
        new = {k: v for k, v in rows.items() if k not in found}
        found.update(rows)
        if not new:
            break
        time.sleep(0.4)
    _log(logf, f"total redeemed entries found={len(found)} ok={ok}")
    return found, ok
