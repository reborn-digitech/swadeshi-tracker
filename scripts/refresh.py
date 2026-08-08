#!/usr/bin/env python3
"""Weekly refresh for the Swadeshi Tracker registry.

Pulls the latest quarterly shareholding pattern for every listed company in
data/companies.json straight from NSE's public filings (JSON index + XBRL
document), classifies each holder bucket as Indian or foreign, and writes:

  - site/registry.json   (the full dataset)
  - site/index.html      (site/template.html with the dataset inlined)

No AI anywhere: the XBRL taxonomy itself tags every category as domestic or
foreign, so classification is pure arithmetic. Private companies use the
curated `static` block from companies.json.

If NSE is unreachable for a company, its previous registry entry is reused
and marked stale rather than dropped.
"""

import json
import re
import sys
import time
import urllib.request
import http.cookiejar
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "companies.json"
SITE = ROOT / "site"
REGISTRY = SITE / "registry.json"
TEMPLATE = SITE / "template.html"
INDEX = SITE / "index.html"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MASTER_URL = ("https://www.nseindia.com/api/corporate-share-holdings-master"
              "?index=equities&symbol={symbol}")

# XBRL contexts we read. Each is a subtotal in the SEBI shareholding format,
# so the buckets below never double-count.
CTX = {
    "promoter_indian": "Indian_ContextI",
    "promoter_foreign": "Foreign_ContextI",
    "dii": "InstitutionsDomestic_ContextI",
    "fii": "InstitutionsForeign_ContextI",
    "govt": "Governments_ContextI",
    "nri": "NonResidentIndians_ContextI",
    "foreign_nationals": "ForeignNationals_ContextI",
    "foreign_companies": "ForeignCompanies_ContextI",
    "total": "ShareholdingPattern_ContextI",
}


def build_opener():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", UA),
        ("Accept", "application/json, text/html, */*"),
        ("Accept-Language", "en-US,en;q=0.9"),
        ("Referer", "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern"),
    ]
    return opener


def fetch(opener, url, tries=3):
    last = None
    for i in range(tries):
        try:
            with opener.open(url, timeout=30) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - retry any network hiccup
            last = e
            time.sleep(2 * (i + 1))
    raise last


def warm_up(opener):
    # NSE's API wants cookies from a page visit first; a 403/blocked homepage
    # still usually sets them, so ignore failures here.
    try:
        fetch(opener, "https://www.nseindia.com/", tries=1)
    except Exception:
        pass


def parse_xbrl_percentages(xml_text):
    vals = dict(re.findall(
        r'<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares'
        r' contextRef="([^"]+)"[^>]*>([^<]+)<', xml_text))
    total = float(vals.get(CTX["total"], 1) or 1)
    out = {}
    for key, ctx in CTX.items():
        if key == "total":
            continue
        try:
            out[key] = float(vals.get(ctx, 0) or 0) / total * 100.0
        except ValueError:
            out[key] = 0.0
    # Depository-receipt custodian holdings (rare) count as foreign.
    custodian = 0.0
    for ctx, v in vals.items():
        if "Custodian" in ctx and ctx.endswith("_ContextI"):
            try:
                custodian += float(v or 0) / total * 100.0
            except ValueError:
                pass
    out["custodian"] = custodian
    return out


def r1(x):
    return round(x * 10) / 10


def classify(company, pcts, as_of):
    override = company.get("promoter_override")
    prom_in = pcts["promoter_indian"]
    prom_for = pcts["promoter_foreign"]
    foreign_inst = pcts["fii"] + pcts["foreign_nationals"] + pcts["foreign_companies"] + pcts["custodian"]

    if override and override.get("origin") == "indian":
        prom_in, prom_for = prom_in + prom_for, 0.0

    foreign_total = prom_for + foreign_inst
    indian_total = 100.0 - foreign_total

    rows = []
    if prom_in > 0.05:
        label = (override or {}).get("label") or company.get("promoter_label_indian") or "Indian promoters"
        rows.append({"holder": label, "pct": r1(prom_in), "origin": "indian"})
    if prom_for > 0.05:
        label = company.get("promoter_label_foreign") or f"{company['parent_company']} (promoter)"
        rows.append({"holder": label, "pct": r1(prom_for), "origin": "foreign"})
    if foreign_inst > 0.05:
        rows.append({"holder": "FII / FPI & foreign institutions", "pct": r1(foreign_inst), "origin": "foreign"})
    if pcts["dii"] > 0.05:
        rows.append({"holder": "DII (mutual funds, insurers, banks)", "pct": r1(pcts["dii"]), "origin": "indian"})
    if pcts["govt"] > 0.05:
        rows.append({"holder": "Government holdings", "pct": r1(pcts["govt"]), "origin": "indian"})
    public = 100.0 - sum(row["pct"] for row in rows)
    if public > 0.05:
        rows.append({"holder": "Indian public & others (incl. NRIs)", "pct": r1(public), "origin": "indian"})

    return {
        "indian_pct": r1(indian_total),
        "foreign_pct": r1(foreign_total),
        "breakdown": rows[:6],
        "as_of": as_of,
        "confidence": "high",
        "sources": [f"NSE shareholding pattern ({as_of})"],
    }


def fetch_listed(opener, company, old_by_id):
    symbol = company["nse_symbol"]
    from urllib.parse import quote
    master = json.loads(fetch(opener, MASTER_URL.format(symbol=quote(symbol))))
    if not master:
        raise RuntimeError(f"no filings returned for {symbol}")
    latest = master[0]
    as_of = "quarter ended " + latest["date"].title()
    xml_text = fetch(opener, latest["xbrl"]).decode("utf-8", errors="replace")
    pcts = parse_xbrl_percentages(xml_text)
    ownership = classify(company, pcts, as_of)
    # Sanity: promoter+public should cover ~100%; a wildly off total means a
    # parsing problem, so fall back to the previous entry instead of shipping it.
    check = (pcts["promoter_indian"] + pcts["promoter_foreign"] + pcts["dii"]
             + pcts["fii"] + pcts["govt"] + pcts["nri"])
    if not 20 <= check <= 101:
        raise RuntimeError(f"implausible parse for {symbol} (subtotals {check:.1f}%)")
    return ownership


def company_entry(company, ownership, stale=False):
    entry = {
        "company_id": company["id"],
        "operating_company": company["name"],
        "parent_company": company["parent_company"],
        "parent_country": company["parent_country"],
        "listed": company["listed"],
        "listing_note": company.get("listing_note", ""),
        "company_notes": company.get("company_notes", ""),
        **ownership,
    }
    override = company.get("promoter_override")
    if override:
        entry["override_note"] = override["note"]
    if stale:
        entry["stale"] = True
    return entry


def main():
    config = json.loads(DATA.read_text())
    companies = config["companies"]

    old_by_id = {}
    if REGISTRY.exists():
        try:
            old = json.loads(REGISTRY.read_text())
            old_by_id = {c["company_id"]: c for c in old.get("companies", [])}
        except Exception:
            pass

    opener = build_opener()
    warm_up(opener)

    results = []
    failures = []
    for company in companies:
        if company["listed"]:
            try:
                ownership = fetch_listed(opener, company, old_by_id)
                results.append(company_entry(company, ownership))
                print(f"  ok   {company['nse_symbol']:12s} "
                      f"indian {ownership['indian_pct']:5.1f}%  ({ownership['as_of']})")
            except Exception as e:
                prev = old_by_id.get(company["id"])
                if prev:
                    prev = dict(prev)
                    prev["stale"] = True
                    results.append(prev)
                    print(f"  KEEP {company.get('nse_symbol', '?'):12s} fetch failed ({e}); reusing previous data")
                else:
                    failures.append((company["id"], str(e)))
                    print(f"  FAIL {company.get('nse_symbol', '?'):12s} {e}")
            time.sleep(0.4)
        else:
            s = company["static"]
            ownership = {
                "indian_pct": s["indian_pct"],
                "foreign_pct": s["foreign_pct"],
                "breakdown": s["breakdown"],
                "as_of": s["as_of"],
                "confidence": s["confidence"],
                "sources": s["sources"],
            }
            results.append(company_entry(company, ownership))

    by_id = {c["company_id"]: c for c in results}
    brands = []
    for company in companies:
        entry = by_id.get(company["id"])
        if not entry:
            continue
        for b in company["brands"]:
            brands.append({
                "product": b["product"],
                "brand": b["brand"],
                "aliases": b["aliases"],
                "notes": b.get("notes", ""),
                **entry,
            })

    registry = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": ("Listed-company splits come from NSE quarterly shareholding filings; "
                 "private companies are curated from public records. NRIs are counted "
                 "on the Indian side."),
        "companies": results,
        "brands": brands,
    }

    SITE.mkdir(exist_ok=True)
    REGISTRY.write_text(json.dumps(registry, ensure_ascii=False, indent=1))
    print(f"\nWrote {REGISTRY} — {len(brands)} brands across {len(results)} companies.")

    if TEMPLATE.exists():
        payload = json.dumps(registry, ensure_ascii=False).replace("</", "<\\/")
        html = TEMPLATE.read_text()
        html = html.replace("__REGISTRY_JSON__", payload)
        INDEX.write_text(html)
        print(f"Wrote {INDEX}.")

    if failures:
        print(f"\n{len(failures)} companies failed with no previous data to fall back on:")
        for cid, err in failures:
            print(f"  - {cid}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
