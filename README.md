# Swadeshi Tracker

**Who owns the shelf?** Type any product sold in India and get its Indian‑vs‑foreign
ownership split instantly — no AI at query time, ever. Answers come from a registry
rebuilt weekly from NSE quarterly shareholding filings and curated public records.

## How it works

```
data/companies.json        curated: ~55 listed + ~39 private companies, 260+ brand aliases
        │
scripts/refresh.py         weekly: pulls each listed company's latest quarterly
        │                  shareholding XBRL from NSE, classifies every holder
        │                  bucket as Indian or foreign (the filing taxonomy
        │                  already tags this — pure arithmetic, no AI)
        ▼
site/registry.json         the generated dataset
site/index.html            template.html with the dataset inlined — the whole
                           site is one static file with local fuzzy search
```

- **Listed companies** (HUL, Britannia, ITC, Tata Consumer…): split computed from the
  latest quarterly filing. Foreign promoters, FII/FPI and foreign institutions count as
  foreign; Indian promoters, DII, government and resident public count as Indian.
  NRIs are counted on the Indian side.
- **Ultimate-control overrides**: e.g. Britannia's promoter stake sits in a UK entity but
  is controlled by the Indian Wadia group — counted Indian, with the caveat shown on the card.
- **Private companies** (Parle, Amul, PepsiCo India, Haldiram's…): curated static entries
  with confidence levels, since they file no quarterly patterns.
- **Failure-safe**: if NSE can't be reached for a company, its previous entry is kept and
  marked stale rather than dropped.

## Running locally

```bash
python3 scripts/refresh.py        # rebuild site/ from live NSE data (stdlib only, no deps)
python3 -m http.server 8737 --directory site
```

## Deployment

`.github/workflows/refresh.yml` refreshes the registry every Sunday 03:00 UTC,
commits the result, and deploys `site/` to GitHub Pages. Trigger it manually from
the Actions tab any time.

## Adding brands

Edit `data/companies.json` — add an alias to an existing company's `brands`, or a new
company (with `nse_symbol` if listed, or a `static` block if private) — then re-run
`scripts/refresh.py` or push to `main`.

*General information, not investment advice. Ownership can change between filings.*
