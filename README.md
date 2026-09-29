# MIRAE-ELS-CHECK

미래에셋증권 공모 ELS 조기상환 조회 사이트. 회차번호를 입력하면 전일 종가 기준으로
조기상환 가능 여부·예상 상환 차수·수익률·낙인 여부를 보여줍니다.

## Files
- `index.html` — site page (GitHub Pages)
- `scripts/collect.py` — daily collector (products + prices + judgment)
- `data/els.json` — site data (auto-generated)
- `data/products.json` — parsed product cache (auto-generated)
- `data/scan_state.json` — ISIN scan cursor (auto-generated)
- `data/seed_isins.txt` — seed ISINs for discovery (editable)
- `data/status.json` — last run log
- `.github/workflows/update.yml` — schedule: weekdays 07:35 KST

## First run
1. Actions > update-els-data > Run workflow > full_scan = 1, max_probes = 1800
2. Repeat step 1 until status.json shows `new: 0`
3. Daily schedule keeps it up to date afterwards

## Manual settings (Settings > Secrets, optional)
- `KSD_API_KEY` — reserved for KSD open API (not required for now)
