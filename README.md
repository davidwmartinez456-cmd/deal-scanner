# AI Deal Scanner

Type in a property address and get a buy / pass decision in under a minute.

The scanner uses Claude with web search to pull the sale price, comparable rents, taxes and insurance for an address, then runs the numbers through a deterministic underwriting engine. The model gathers the inputs; the math is plain Python, so every result can be traced and checked by hand.

I built it to screen small multifamily and mixed-use deals in northern New Jersey faster than I could in a spreadsheet.

<!-- Add a screenshot here: ![Scorecard](docs/scorecard.png) -->

## What it does

**8-metric scorecard.** Each deal is scored pass/fail against fixed floors:

| Metric | Floor |
|---|---|
| 1% Rule | rent ≥ 1% of price |
| 50% Rule | NOI checked against half of gross income |
| NOI | positive |
| Cap Rate | ≥ 7.0% |
| Cash-on-Cash | ≥ 8.0% |
| DSCR | ≥ 1.20 |
| LTV | ≤ 80% |
| Positive Leverage | cap rate above borrowing rate |

**Negotiation target.** Works backwards from NOI to the highest purchase price that clears cap rate, DSCR and positive leverage at the same time. The tightest constraint sets the offer.

**Five exit strategies**, each with its own endpoint and step-by-step footnotes showing the arithmetic:

- Section 8 (HUD Small Area Fair Market Rents, with a built-in table for 15 NJ zip codes)
- Short-term rental
- Value-add long-term rental
- Flip
- HELOC / equity pull

**Mixed-use support.** Enter commercial and residential units separately; the scanner reports commercial rent per square foot so it can be checked against local benchmarks.

**Risk and market tiers.** Optional deeper passes score five risks against five rewards with weighted ratios, and pull employer, demographic and supply data for the zip code.

**Due diligence checklist.** Generated per deal, with extra items for mixed-use properties and marginal scores.

**Gmail listing parser** (`gmail_listings.py`). Reads MLS auto-prospecting emails through the Gmail API (read-only scope) and extracts address and price for each listing. It currently runs on its own; feeding its output straight into the scorer is the next step.

## How it is built

```
index.html  ──POST──▶  server.py (Flask)
                          │
                          ├─ fetch_property_data()   Claude + web search → JSON inputs
                          ├─ calc_financials()       NOI, debt service, cash flow
                          ├─ 7-year projection       rent and expense growth at 3% a year
                          ├─ build_scorecard()       8 pass/fail metrics
                          ├─ calc_negotiation_target()
                          └─ /api/strategy/*         section8, str, ltr, flip, heloc
```

Design choices:

- **The model never does the math.** It returns structured JSON inputs with a source for each figure. All calculations are deterministic Python.
- **User input overrides the model.** Rent and price overrides take precedence over anything the model finds.
- **Token use is tiered.** The basic scan makes one model call. Reference data such as the NJ rent table is hardcoded so common lookups cost nothing.
- **Underwriting assumptions live in one config block** (`BASE_CONFIG`, `LOAN_DEFAULTS`) at the top of `server.py`.

## Run it

Requires Python 3.9+ and an Anthropic API key.

```bash
git clone <this-repo-url>
cd deal-scanner
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then paste your API key into .env
python server.py
```

Open `index.html` in a browser. The page calls the server at `http://127.0.0.1:5000`.

For the Gmail parser, create an OAuth client in Google Cloud Console, save it as `credentials.json` in this folder, and run `python gmail_listings.py`. The first run opens a browser window to authorize read-only access.

## Limits

- Property data comes from web search, so it can be wrong or stale. Verify price, rent and taxes before acting on a result.
- Rent tables and benchmarks are tuned for northern New Jersey.
- This is a screening tool for my own use, not financial advice.

## Roadmap

- Connect the Gmail parser to the scorer so new listings are scored automatically
- Unit tests for the financial engine
- Save scan history
