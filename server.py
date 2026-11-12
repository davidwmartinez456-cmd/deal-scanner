import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import anthropic

load_dotenv()
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
app = Flask(__name__)
CORS(app)

BASE_CONFIG = {
    # ── Scorecard floors (from DM Master Doc) ─────────────────────────
    "min_cap_rate": 7.0, "min_coc": 8.0, "min_dscr": 1.2,
    # ── Operating expense defaults (from DM Master Doc) ───────────────
    "vacancy_rate": 7,
    "mgmt_fee": 10,        # Master Doc: 10% even when self-managing (prices in your time)
    "maintenance": 8,      # Master Doc: 8% for HQS-compliant properties
    "closing_costs": 3,    # Master Doc: 3% standard NJ buyer total
    # ── Projection settings ───────────────────────────────────────────
    "rent_accelerator": 3, "expense_accelerator": 3, "projection_years": 7,
    # ── Strategy settings ─────────────────────────────────────────────
    "flip_margin_floor": 20, "flip_carry_months": 9,
    "heloc_equity_threshold": 25, "heloc_reno_pct": 7, "heloc_arv_multiplier": 1.5,
    "nj_construction_floor": 400, "pa_construction_floor": 300,
    "dscr_min_ratio": 1.25
}

LOAN_DEFAULTS = {
    # ── Default: investor standard from DM Master Doc (25% down, 6.5%) ─
    "investment":   {"down": 25, "rate": 6.5,  "label": "Investment Loan"},
    "conventional": {"down": 5,  "rate": 6.5,  "label": "Conventional"},
    "all_cash":     {"down": 100,"rate": 0,     "label": "All Cash"},
    "dscr":         {"down": 25, "rate": 7.75,  "label": "DSCR Loan"},
    "hard_money":   {"down": 15, "rate": 12.0,  "label": "Hard Money"},
}

NJ_SAFMR_2BR = {
    "07302":{"area":"Downtown JC / Waterfront","safmr":4140},
    "07307":{"area":"The Heights","safmr":2720},
    "07306":{"area":"Journal Square","safmr":2530},
    "07305":{"area":"Greenville / Lafayette","safmr":2340},
    "07304":{"area":"Bergen-Lafayette","safmr":2340},
    "07303":{"area":"Downtown JC","safmr":3800},
    "08861":{"area":"Perth Amboy","safmr":2276},
    "07036":{"area":"Linden","safmr":2200},
    "07202":{"area":"Elizabeth","safmr":2180},
    "07208":{"area":"Elizabeth (Elmora)","safmr":2180},
    "07065":{"area":"Rahway","safmr":2150},
    "07002":{"area":"Bayonne","safmr":2400},
    "07087":{"area":"Union City / West New York","safmr":2600},
    "07093":{"area":"West New York","safmr":2550},
    "07047":{"area":"North Bergen","safmr":2500},
}

POCONO_ZIPS = {"18466","18347","18372","18370","18344","18326","18330",
               "18332","18334","18346","18348","18353","18355","18360"}

# ── CORE FINANCIAL ENGINE ────────────────────────────────────────────
# Linden/NJ commercial rent benchmarks ($/sqft/year) for cross-check
COMMERCIAL_RENT_PSF = {
    "retail_low":    18,   # low-traffic storefront
    "retail_mid":    24,   # standard Linden/Union County retail
    "retail_high":   32,   # corner, high-visibility
    "office":        20,
    "flex_industrial": 14,
}

def calc_unit_income(units, config):
    """
    units: list of dicts with keys:
        type: 'residential' | 'commercial'
        monthly_rent: number
        sqft: number (optional, for commercial cross-check)
        vacancy_override: optional override for vacancy pct
    Returns: total gross annual income, itemized NOI by unit type,
             combined expense breakdown
    """
    res_gross  = 0
    com_gross  = 0
    com_sqft_total = 0

    for u in units:
        annual = u['monthly_rent'] * 12
        if u['type'] == 'commercial':
            com_gross += annual
            com_sqft_total += u.get('sqft', 0)
        else:
            res_gross += annual

    total_gross = res_gross + com_gross

    # Residential expenses — standard percentages
    res_vacancy  = res_gross * (config['vacancy_rate'] / 100)
    res_mgmt     = res_gross * (config['mgmt_fee'] / 100)
    res_maint    = res_gross * (config['maintenance'] / 100)

    # Commercial expenses — lower vacancy (3% — NNN/gross leases are 3-5yr terms)
    # Management 4% (less turnover, simpler management)
    # Maintenance 2% (commercial tenants responsible for interior in NNN)
    com_vacancy  = com_gross * 0.03
    com_mgmt     = com_gross * 0.04
    com_maint    = com_gross * 0.02

    total_op_exp = res_vacancy + res_mgmt + res_maint + com_vacancy + com_mgmt + com_maint

    # Commercial $/sqft cross-check
    com_psf_check = None
    if com_sqft_total > 0 and com_gross > 0:
        com_psf_check = round(com_gross / com_sqft_total, 2)

    return dict(
        res_gross=res_gross, com_gross=com_gross,
        total_gross=total_gross,
        res_vacancy=res_vacancy, res_mgmt=res_mgmt, res_maint=res_maint,
        com_vacancy=com_vacancy, com_mgmt=com_mgmt, com_maint=com_maint,
        total_op_exp=total_op_exp,
        com_sqft=com_sqft_total, com_psf_annual=com_psf_check
    )


def calc_financials(price, rent, taxes, insurance, water,
                    down_pct, rate, loan_type, config,
                    mixed_use_breakdown=None):
    """
    mixed_use_breakdown: if provided, overrides single rent with
    unit-level income calc. Format: list of unit dicts from calc_unit_income.
    """
    if mixed_use_breakdown:
        ui = calc_unit_income(mixed_use_breakdown, config)
        annual_gross = ui['total_gross']
        vacancy_loss = ui['res_vacancy'] + ui['com_vacancy']
        mgmt_cost    = ui['res_mgmt']   + ui['com_mgmt']
        maint        = ui['res_maint']  + ui['com_maint']
    else:
        ui = None
        annual_gross  = rent * 12
        vacancy_loss  = annual_gross * (config['vacancy_rate'] / 100)
        mgmt_cost     = annual_gross * (config['mgmt_fee'] / 100)
        maint         = annual_gross * (config['maintenance'] / 100)

    total_exp  = vacancy_loss + mgmt_cost + maint + taxes + insurance + water
    noi        = annual_gross - total_exp

    down       = price * (down_pct / 100)
    closing    = price * (config['closing_costs'] / 100)
    total_cash = down + closing
    loan_amt   = price - down
    ltv_pct    = (loan_amt / price) * 100 if price > 0 else 0

    all_cash   = (loan_type == 'all_cash' or down_pct >= 100)
    mr         = (rate / 100) / 12 if rate > 0 else 0
    n          = 30 * 12

    if all_cash or loan_amt <= 0:
        mortgage = 0; annual_debt = 0
    elif mr > 0:
        mortgage    = loan_amt * (mr*(1+mr)**n) / ((1+mr)**n - 1)
        annual_debt = mortgage * 12
    else:
        mortgage = loan_amt / n; annual_debt = mortgage * 12

    annual_cf  = noi - annual_debt
    monthly_cf = annual_cf / 12
    dscr       = (noi / annual_debt) if annual_debt > 0 else None
    dscr_candidate = (annual_debt > 0 and dscr is not None and
                      dscr >= config['dscr_min_ratio'])
    payback_years = total_cash / annual_cf if annual_cf > 0 else None

    result = dict(
        annual_gross=annual_gross, vacancy_loss=vacancy_loss,
        mgmt_cost=mgmt_cost, maint=maint, total_exp=total_exp, noi=noi,
        down=down, closing=closing, total_cash=total_cash,
        loan_amt=loan_amt, ltv_pct=ltv_pct, mortgage=mortgage,
        annual_debt=annual_debt, annual_cf=annual_cf, monthly_cf=monthly_cf,
        dscr=dscr, dscr_candidate=dscr_candidate, all_cash=all_cash,
        payback_years=payback_years, down_pct=down_pct, rate=rate,
        loan_type=loan_type
    )
    if ui:
        result['unit_income'] = ui
    return result


def calc_negotiation_target(noi, annual_gross, down_pct, rate, loan_term, config):
    """
    Reverse-engineers the maximum purchase price that clears ALL THREE
    critical metrics simultaneously: Cap Rate, DSCR, and Positive Leverage.
    The binding constraint (lowest price) wins — most conservative, most defensible.
    """
    results = {}

    # ── Constraint 1: Cap Rate >= min_cap_rate ────────────────────────
    # NOI / Price >= min_cap_rate  →  Price <= NOI / min_cap_rate
    cap_max = noi / (config['min_cap_rate'] / 100) if noi > 0 else 0
    results['cap_rate_max'] = round(cap_max)
    results['cap_rate_constraint'] = f"NOI ${noi:,.0f} ÷ {config['min_cap_rate']}% = ${cap_max:,.0f}"

    # ── Constraint 2: DSCR >= min_dscr ───────────────────────────────
    # NOI / Debt Service >= min_dscr
    # Debt Service = mortgage × 12 = [(Price-Down) × monthly_rate × (1+r)^n / ((1+r)^n - 1)] × 12
    # Solving for Price numerically (binary search — closed form is messy with LTV interaction)
    mr = (rate / 100) / 12
    n  = loan_term * 12
    dp = down_pct / 100

    def annual_debt_at_price(p):
        loan = p * (1 - dp)
        if mr > 0:
            m = loan * (mr*(1+mr)**n) / ((1+mr)**n - 1)
        else:
            m = loan / n
        return m * 12

    def dscr_at_price(p):
        debt = annual_debt_at_price(p)
        return noi / debt if debt > 0 else 999

    # Binary search for max price where DSCR >= min_dscr
    lo, hi = 1, 5_000_000
    for _ in range(50):
        mid = (lo + hi) / 2
        if dscr_at_price(mid) >= config['min_dscr']:
            lo = mid
        else:
            hi = mid
    dscr_max = round(lo)
    results['dscr_max'] = dscr_max
    results['dscr_constraint'] = (
        f"NOI ${noi:,.0f} must cover {config['min_dscr']}x debt service → "
        f"max debt service ${noi/config['min_dscr']:,.0f}/yr → max price ${dscr_max:,.0f}"
    )

    # ── Constraint 3: Positive Leverage (Cap Rate > Borrowing Rate) ──
    # Cap Rate must exceed borrowing rate by at least 0 bps
    # Price <= NOI / rate  (cap rate must equal at least the borrowing rate)
    pos_lev_max = noi / (rate / 100) if rate > 0 else cap_max
    results['pos_lev_max'] = round(pos_lev_max)
    results['pos_lev_constraint'] = (
        f"NOI ${noi:,.0f} ÷ borrowing rate {rate}% = ${pos_lev_max:,.0f} "
        f"(cap rate must exceed {rate}%)"
    )

    # ── Binding constraint: most conservative wins ────────────────────
    ceiling = min(cap_max, dscr_max, pos_lev_max)
    binding = (
        'Cap Rate' if ceiling == cap_max
        else ('DSCR' if ceiling == dscr_max else 'Positive Leverage')
    )

    # Opening offer: 10% below ceiling (negotiating room)
    opening_offer = round(ceiling * 0.90)
    # Aggressive offer: 15% below ceiling
    aggressive_offer = round(ceiling * 0.85)

    # ── At-ceiling metrics validation ────────────────────────────────
    debt_at_ceiling = annual_debt_at_price(ceiling)
    cap_at_ceiling  = (noi / ceiling) * 100 if ceiling > 0 else 0
    dscr_at_ceiling_val = noi / debt_at_ceiling if debt_at_ceiling > 0 else 0
    pos_lev_at_ceiling  = cap_at_ceiling - rate

    results.update({
        'ceiling': round(ceiling),
        'binding_constraint': binding,
        'opening_offer': opening_offer,
        'aggressive_offer': aggressive_offer,
        'at_ceiling': {
            'cap_rate': round(cap_at_ceiling, 2),
            'dscr': round(dscr_at_ceiling_val, 2),
            'positive_leverage': round(pos_lev_at_ceiling, 2),
        }
    })
    return results


def build_due_diligence(address, is_mixed_use, mixed_use_units,
                        score, decision, listing_agent=None):
    """
    Generates a contextual due diligence checklist.
    Mixed-use deals and marginal scores get additional items.
    """
    agent_str = f"listing agent ({listing_agent})" if listing_agent else "listing agent"

    # Universal items — every deal
    items = [
        {"category": "Title & Legal",
         "items": [
             "Order title search — confirm no liens, judgments, or encumbrances",
             "Verify ownership matches seller on deed (NJ county records)",
             "Confirm property is in an LLC-compatible title structure",
             "Check for any open permits or violations (municipal records)",
         ]},
        {"category": "Physical Inspection",
         "items": [
             "Schedule pre-inspection before offer (HQS standards if Section 8 target)",
             "Inspect roof, HVAC, plumbing, electrical — estimate useful life",
             "Verify sqft and unit count matches listing",
             "Check for any environmental flags (pre-1978 = lead paint disclosure required)",
         ]},
        {"category": "Financial Verification",
         "items": [
             f"Request trailing 12-month income and expense statement from {agent_str}",
             "Verify actual rents against bank statements — not just lease documents",
             "Confirm property tax figure on county tax portal (not Ownerly estimate)",
             "Get current insurance quote — do not use AI estimate for underwriting",
         ]},
    ]

    # Mixed-use specific items
    if is_mixed_use:
        commercial_items = [
            f"Request rent roll showing ALL units from {agent_str} — verify each unit's actual rent",
            "Request copies of all active leases — confirm term, expiration, renewal options",
            "Verify commercial tenant type (retail, office, service) — affects lease structure",
            "Confirm whether commercial lease is NNN, gross, or modified gross",
            "Get professional floor plan or physical measurement of commercial unit sqft",
            "Check commercial tenant's business license and certificate of occupancy",
            "Verify commercial unit meets zoning for current use (Linden zoning office)",
            "Ask if commercial tenant is current on rent — request 12-month payment history",
            "If month-to-month commercial: factor 3% vacancy and lease-up risk into model",
        ]
        items.append({"category": "Commercial Unit — Mixed-Use Specific", "items": commercial_items})

        # Add unit-specific items
        if mixed_use_units:
            for i, u in enumerate(mixed_use_units, 1):
                if u.get('sqft', 0) == 0:
                    items[2]["items"].append(
                        f"Unit {i} ({u['type']}): sqft not entered — get professional measurement "
                        f"to verify ${u['monthly_rent']*12/max(u.get('sqft',1),1):.0f}/sqft/yr estimate"
                    )

    # Additional items for marginal deals
    if score <= 4:
        items.append({"category": "Value-Add Verification",
         "items": [
             "Get minimum 2 GC bids for any planned renovation before closing",
             "Verify rent ceiling against 3 comparable sources in same zip — not adjacent zip",
             "Confirm renovation payback period using ACTUAL contractor cost, not tool estimate",
             "Request supply pipeline data: new permits issued in this zip in last 24 months",
         ]})

    return items


def build_scorecard(price, rent, f, config, rate=0.0, est_market_value=0):
    one_pct   = (rent / price) * 100 if price > 0 else 0
    cap_rate  = (f['noi'] / price) * 100 if price > 0 else 0
    coc       = (f['annual_cf'] / f['total_cash']) * 100 if f['total_cash'] > 0 else 0
    dscr      = f['dscr']
    ltv_pct   = f['ltv_pct']
    all_cash  = f['all_cash']

    # Fix 3 — Positive Leverage replaces GRM (from DM Master Doc metric 8)
    # Formula: Cap Rate > Borrowing Rate → debt amplifies returns
    # If negative, every dollar borrowed destroys value
    pos_lev_spread = cap_rate - rate if rate > 0 else None
    if all_cash or rate == 0:
        pos_lev_val    = "N/A (All Cash)"
        pos_lev_pass   = True
        pos_lev_signal = "No leverage — full equity"
    elif pos_lev_spread is not None and pos_lev_spread > 0:
        pos_lev_val    = f"+{pos_lev_spread:.2f}%"
        pos_lev_pass   = True
        pos_lev_signal = f"POSITIVE — Cap {cap_rate:.1f}% > Rate {rate:.1f}%"
    else:
        pos_lev_val    = f"{pos_lev_spread:.2f}%" if pos_lev_spread is not None else "—"
        pos_lev_pass   = False
        pos_lev_signal = f"NEGATIVE — Rate {rate:.1f}% > Cap {cap_rate:.1f}% — debt destroys value"

    # Fix 4 — Cap Rate at market value (for existing owners evaluating hold/sell)
    cap_at_market = (f['noi'] / est_market_value) * 100 if est_market_value > 0 else None

    scorecard = [
        {"metric":"1% Rule","value":f"{one_pct:.2f}%","threshold":">= 1.0%",
         "pass":one_pct>=1.0,"signal":"PASSES >= 1%" if one_pct>=1.0 else "FAILS — rent too low for price"},
        {"metric":"50% Rule NOI","value":f"${f['annual_gross']*0.5:,.0f}","threshold":"vs actual NOI",
         "pass":f['noi']>=f['annual_gross']*0.4,"signal":"NOI looks solid" if f['noi']>=f['annual_gross']*0.4 else "Check expenses"},
        {"metric":"NOI","value":f"${f['noi']:,.0f}","threshold":"> $0",
         "pass":f['noi']>0,"signal":"POSITIVE" if f['noi']>0 else "NEGATIVE"},
        {"metric":"Cap Rate","value":f"{cap_rate:.2f}%","threshold":">= 7.0%",
         "pass":cap_rate>=config['min_cap_rate'],
         "signal":"STRONG >= 7%" if cap_rate>=7 else ("MARGINAL 5-7%" if cap_rate>=5 else "WEAK < 5%"),
         "sub": f"At market value: {cap_at_market:.2f}%" if cap_at_market else None},
        {"metric":"Cash-on-Cash","value":f"{coc:.2f}%","threshold":">= 8.0%",
         "pass":coc>=config['min_coc'],"signal":"SOLID >= 8%" if coc>=8 else ("WEAK 0-8%" if coc>=0 else "NEGATIVE")},
        {"metric":"DSCR","value":"N/A (All Cash)" if all_cash else f"{dscr:.2f}" if dscr else "—",
         "threshold":"N/A" if all_cash else ">= 1.20",
         "pass":True if all_cash else (dscr>=config['min_dscr'] if dscr else False),
         "signal":"Not applicable — no debt" if all_cash else ("BANK APPROVES >= 1.2" if dscr and dscr>=1.2 else ("BREAK EVEN" if dscr and dscr>=1.0 else "BANK REJECTS < 1.0"))},
        {"metric":"LTV","value":"0% (All Cash)" if all_cash else f"{ltv_pct:.0f}%","threshold":"<= 80%",
         "pass":True if all_cash else ltv_pct<=80,
         "signal":"No leverage — full equity" if all_cash else ("SAFE <= 80%" if ltv_pct<=80 else ("ELEVATED 80-90%" if ltv_pct<=90 else "HIGH RISK > 90%"))},
        # Fix 3 — Positive Leverage as metric 8 (replaces GRM per DM Master Doc)
        {"metric":"Positive Leverage","value":pos_lev_val,"threshold":"Cap > Rate",
         "pass":pos_lev_pass,"signal":pos_lev_signal},
    ]
    return scorecard, cap_rate, coc, cap_at_market


def fetch_property_data(address):
    r = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=2000,
        tools=[{"type":"web_search_20250305","name":"web_search"}],
        messages=[{"role":"user","content":f"""Real estate research for: {address}
Search Zillow, Redfin, county tax records.
1. PURCHASE PRICE: most recent actual sale from public records NOT Zestimate. If none, listing price. State which.
2. MONTHLY RENT: median comparable rentals 0.5mi, same bed/bath. NOT lowest.
3. ANNUAL TAXES: actual county tax bill.
4. ANNUAL INSURANCE: estimate by property type/location.
5. WATER/SEWER: estimate or 0.
6. ESTIMATED CURRENT VALUE: Zillow Zestimate or Redfin estimate (separate from sale price).

Return ONLY JSON:
{{"purchase_price":<n>,"monthly_rent":<n>,"annual_taxes":<n>,
"annual_insurance":<n>,"water_sewer":<n>,
"estimated_current_value":<n>,
"bedrooms":<n>,"bathrooms":<n>,"sqft":<n>,
"year_built":<n>,"property_type":"<type>",
"zip_code":"<5-digit>","state":"<2-letter>",
"days_on_market":<n>,
"sources":{{"price_source":"<src>","rent_source":"<src>",
"tax_source":"<src>","insurance_source":"<src>","value_source":"<src>"}}}}"""}])
    raw = "".join(b.text for b in r.content if hasattr(b,"text"))
    return json.loads(raw[raw.find("{"):raw.rfind("}")+1])


# ── BASE ANALYSIS ────────────────────────────────────────────────────
@app.route('/api/analyze', methods=['POST'])
def analyze():
    body       = request.json
    address    = body.get('address')
    tier       = body.get('tier','basic')
    loan_type  = body.get('loan_type','investment')
    down_pct   = float(body.get('down_pct', LOAN_DEFAULTS.get(loan_type,{}).get('down',25)))
    rate       = float(body.get('rate', LOAN_DEFAULTS.get(loan_type,{}).get('rate',6.5)))
    rent_override  = float(body['rent_override']) if body.get('rent_override') else None
    price_override = float(body['price_override']) if body.get('price_override') else None
    mixed_use_units = body.get('mixed_use_units', None)  # list of unit dicts or None

    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"status":"error","message":"API Key Missing from .env"}),500

    price_mode = body.get('price_mode', 'acquisition')

    log=[f"Searching for: {address}",
         f"Mode: {price_mode.upper()}",
         f"Loan: {LOAN_DEFAULTS.get(loan_type,{}).get('label',loan_type)}, {down_pct}% down, {rate}% rate"]
    try:
        data      = fetch_property_data(address)
        sale_price    = float(data.get("purchase_price",0))
        est_value     = float(data.get("estimated_current_value",0))
        ai_rent   = float(data.get("monthly_rent",0))
        taxes     = float(data.get("annual_taxes",0))
        insurance = float(data.get("annual_insurance",0))
        water     = float(data.get("water_sewer",0))
        sources   = data.get("sources",{})
        if sale_price==0 or ai_rent==0:
            return jsonify({"status":"error","message":"Could not find property data."}),400

        # Apply overrides — user knowledge beats AI estimate
        if mixed_use_units and len(mixed_use_units) > 0:
            # Validate and clean units
            for u in mixed_use_units:
                u['monthly_rent'] = float(u.get('monthly_rent', 0))
                u['sqft'] = float(u.get('sqft', 0))
                if u['type'] not in ('commercial', 'residential'):
                    u['type'] = 'residential'
            total_monthly = sum(u['monthly_rent'] for u in mixed_use_units)
            rent = total_monthly
            log.append(f"⚡ Mixed-use mode: {len(mixed_use_units)} units — total ${rent:,.0f}/mo")
            for i, u in enumerate(mixed_use_units, 1):
                psf = f" ({u['sqft']:.0f} sqft = ${u['monthly_rent']*12/u['sqft']:.2f}/sqft/yr)" if u.get('sqft', 0) > 0 else ""
                log.append(f"  Unit {i}: {u['type'].title()} — ${u['monthly_rent']:,.0f}/mo{psf}")
        elif rent_override and rent_override > 0:
            rent = rent_override
            log.append(f"⚡ Rent override applied: ${rent:,.0f}/mo (AI found ${ai_rent:,.0f}/mo)")
        else:
            rent = ai_rent
            log.append(f"Rent: {sources.get('rent_source','web search')}")

        # Price mode + price override
        if price_override and price_override > 0:
            analysis_price = price_override
            price_mode_note = f"Price override: ${analysis_price:,.0f} (AI found market ${est_value:,.0f}, last sale ${sale_price:,.0f})"
            log.append(f"⚡ Price override applied: ${analysis_price:,.0f}")
        elif price_mode == 'acquisition':
            analysis_price = est_value if est_value > 0 else sale_price
            price_mode_note = f"Acquisition mode: using current market value ${analysis_price:,.0f}"
        else:
            analysis_price = sale_price
            price_mode_note = f"Portfolio mode: using original purchase price ${analysis_price:,.0f}"

        price = analysis_price

        log.append(f"Price: {sources.get('price_source','web search')} (last sale: ${sale_price:,.0f})")
        log.append(price_mode_note)
        log.append(f"Tax: {sources.get('tax_source','web search')}")
        log.append(f"Insurance: {sources.get('insurance_source','estimated')}")
        if sources.get("value_source"):
            log.append(f"Est. Value: {sources.get('value_source')}")

        f = calc_financials(price,rent,taxes,insurance,water,down_pct,rate,loan_type,BASE_CONFIG,
                            mixed_use_breakdown=mixed_use_units if mixed_use_units else None)
        scorecard,cap_rate,coc,cap_at_market = build_scorecard(
            price, rent, f, BASE_CONFIG,
            rate=rate,
            est_market_value=est_value if price_mode=='acquisition' and sale_price!=analysis_price else 0
        )
        total_pass = sum(1 for s in scorecard if s["pass"])

        if   total_pass>=7: verdict,decision="STRONG DEAL — move forward","BUY"
        elif total_pass>=5: verdict,decision="SOLID — negotiate or proceed","BUY"
        elif total_pass>=3: verdict,decision="MARGINAL — value-add only","REVIEW"
        else:               verdict,decision="WEAK — not investable as-is","PASS"

        # DSCR loan candidate badge — fires even if main loan type fails DSCR
        dscr_badge = None
        if not f['all_cash'] and f['noi'] > 0:
            # Test at DSCR loan params (25% down, 7.75%)
            f_dscr = calc_financials(price,rent,taxes,insurance,water,25,7.75,'dscr',BASE_CONFIG)
            if f_dscr['dscr'] and f_dscr['dscr'] >= BASE_CONFIG['dscr_min_ratio']:
                dscr_badge = {
                    "qualifies": True,
                    "dscr_ratio": round(f_dscr['dscr'],2),
                    "note": f"NOI ${f['noi']:,.0f} covers {f_dscr['dscr']:.2f}x DSCR debt service at 25% down / 7.75%. Qualifies without W2."
                }

        ra,ea = BASE_CONFIG['rent_accelerator']/100, BASE_CONFIG['expense_accelerator']/100
        projection=[]
        for yr in range(1,BASE_CONFIG['projection_years']+1):
            yr_rent = f['annual_gross']*(1+ra)**(yr-1)
            yr_exp  = (yr_rent*BASE_CONFIG['vacancy_rate']/100 + taxes*(1+ea)**(yr-1) +
                       insurance*(1+ea)**(yr-1) + yr_rent*BASE_CONFIG['maintenance']/100 +
                       yr_rent*BASE_CONFIG['mgmt_fee']/100 + water*(1+ea)**(yr-1))
            yr_noi  = yr_rent - yr_exp
            yr_cf   = yr_noi - f['annual_debt']
            yr_coc  = (yr_cf/f['total_cash'])*100 if f['total_cash']>0 else 0
            yr_dscr = yr_noi/f['annual_debt'] if f['annual_debt']>0 else None
            projection.append({
                "year":yr,"gross_rent":round(yr_rent),"total_expenses":round(yr_exp),
                "noi":round(yr_noi),"debt_service":round(f['annual_debt']),
                "net_cash_flow":round(yr_cf),"coc":round(yr_coc,2),
                "dscr":round(yr_dscr,2) if yr_dscr else None
            })

        result={
            "status":"success","secure":True,"decision":decision,"address":address,"tier":tier,
            "price_mode":price_mode,
            "financing":{"loan_type":loan_type,"label":LOAN_DEFAULTS.get(loan_type,{}).get('label',loan_type),
                         "down_pct":down_pct,"rate":rate,"all_cash":f['all_cash']},
            "property":{"bedrooms":data.get("bedrooms",0),"bathrooms":data.get("bathrooms",0),
                        "sqft":data.get("sqft",0),"year_built":data.get("year_built",0),
                        "type":data.get("property_type","unknown"),
                        "zip_code":data.get("zip_code",""),"state":data.get("state",""),
                        "days_on_market":data.get("days_on_market",0)},
            "inputs":{"purchase_price":price,"sale_price":sale_price,
                      "monthly_rent":rent,"annual_gross":f['annual_gross'],
                      "annual_taxes":taxes,"annual_insurance":insurance,"water_sewer":water,
                      "estimated_current_value":est_value,
                      "price_mode_note":price_mode_note,
                      "rent_override":rent_override,
                      "price_override":price_override,
                      "mixed_use_units":mixed_use_units,
                      "is_mixed_use": bool(mixed_use_units)},
            "derived":{"down_payment":f['down'],"loan_amount":f['loan_amt'],
                       "monthly_mortgage":round(f['mortgage']),"annual_debt_service":round(f['annual_debt']),
                       "total_cash_invested":f['total_cash'],"vacancy_loss":f['vacancy_loss'],
                       "mgmt_cost":f['mgmt_cost'],"maintenance":f['maint'],
                       "total_expenses":f['total_exp'],"noi":round(f['noi']),
                       "monthly_cf":round(f['monthly_cf']),"annual_cf":round(f['annual_cf']),
                       "cap_rate":round(cap_rate,2),
                       "cap_rate_at_market":round(cap_at_market,2) if cap_at_market else None,
                       "coc":round(coc,2),
                       "dscr":round(f['dscr'],2) if f['dscr'] else None,
                       "payback_years":round(f['payback_years'],1) if f['payback_years'] else None,
                       "unit_income":f.get('unit_income', None)},
            "scorecard":scorecard,"score":f"{total_pass}/8","verdict":verdict,
            "dscr_badge":dscr_badge,
            "projection":projection,"sources":log,"config_used":BASE_CONFIG,
            "negotiation_target": calc_negotiation_target(
                noi=f['noi'],
                annual_gross=f['annual_gross'],
                down_pct=down_pct,
                rate=rate,
                loan_term=30,
                config=BASE_CONFIG
            ),
            "due_diligence": build_due_diligence(
                address=address,
                is_mixed_use=bool(mixed_use_units),
                mixed_use_units=mixed_use_units,
                score=total_pass,
                decision=decision,
                listing_agent=None
            ),
        }

        if tier in ('risk','intel'):
            try:
                r2=client.messages.create(model="claude-sonnet-4-6",max_tokens=1500,
                    tools=[{"type":"web_search_20250305","name":"web_search"}],
                    messages=[{"role":"user","content":f"""Risk/reward for: {address} (${price:,.0f}, ${rent:,.0f}/mo)
Score 1-5 each. Risk 1=low,5=high. Reward 1=weak,5=strong.
Return ONLY JSON: {{"risks":{{"no_job_driver":{{"score":<n>,"note":"<ev>"}},"weak_demographics":{{"score":<n>,"note":"<ev>"}},"supply_pipeline":{{"score":<n>,"note":"<ev>"}},"rent_ceiling_risk":{{"score":<n>,"note":"<ev>"}},"flood_environmental":{{"score":<n>,"note":"<ev>"}}}},"rewards":{{"discount_to_value":{{"score":<n>,"note":"<ev>"}},"job_driver_proximity":{{"score":<n>,"note":"<ev>"}},"renter_market":{{"score":<n>,"note":"<ev>"}},"rent_growth_trend":{{"score":<n>,"note":"<ev>"}},"constrained_supply":{{"score":<n>,"note":"<ev>"}}}}}}"""}])
                raw2="".join(b.text for b in r2.content if hasattr(b,"text"))
                rr=json.loads(raw2[raw2.find("{"):raw2.rfind("}")+1])
                rw={"no_job_driver":3,"weak_demographics":3,"supply_pipeline":2,"rent_ceiling_risk":3,"flood_environmental":3}
                ww={"discount_to_value":3,"job_driver_proximity":4,"renter_market":3,"rent_growth_trend":3,"constrained_supply":3}
                rs=sum(rr["risks"][k]["score"]*rw[k] for k in rw)/sum(rw.values())
                ws=sum(rr["rewards"][k]["score"]*ww[k] for k in ww)/sum(ww.values())
                ratio=ws/rs if rs>0 else 0
                if ratio>=2.0: rv="EXCEPTIONAL — move fast"
                elif ratio>=1.5: rv="STRONG — proceed"
                elif ratio>=1.2: rv="SOLID — proceed with flex"
                elif ratio>=1.0: rv="MARGINAL — reduce risks"
                else: rv="PASS — risk outweighs reward"
                result["risk_reward"]={"risks":rr["risks"],"rewards":rr["rewards"],
                    "risk_score":round(rs,2),"reward_score":round(ws,2),"ratio":round(ratio,2),"verdict":rv}
            except Exception as e:
                result["risk_reward_error"]=str(e)

        if tier=='intel':
            try:
                r3=client.messages.create(model="claude-sonnet-4-6",max_tokens=2500,
                    tools=[{"type":"web_search_20250305","name":"web_search"}],
                    messages=[{"role":"user","content":f"""Deep market research for: {address}
Return ONLY JSON:
{{"jobs":{{"nearest_employer":"<n>","industry":"<t>","headcount_estimate":<n>,"distance_miles":<n>,"avg_wage":<n>,"max_affordable_rent":<n>,"source":"<s>"}},"demographics":{{"zip_code":"<z>","pct_renter":<d>,"median_income":<n>,"median_rent_2br":<n>,"population_trend":"<t>","source":"<s>"}},"rent_ceiling":{{"comp_low":<n>,"comp_median":<n>,"comp_high":<n>,"new_construction_rent":<n>,"source":"<s>"}},"supply":{{"new_permits_24mo":<n>,"new_apt_projects":"<d>","vacancy_zip":<d>,"days_on_market":<n>,"source":"<s>"}},"qualitative":{{"school_rating":<n>,"flood_zone":"<z>","crime_level":"<l>","walkability":<n>,"source":"<s>"}}}}"""}])
                raw3="".join(b.text for b in r3.content if hasattr(b,"text"))
                result["market_intel"]=json.loads(raw3[raw3.find("{"):raw3.rfind("}")+1])
            except Exception as e:
                result["market_intel_error"]=str(e)

        return jsonify(result)
    except json.JSONDecodeError:
        return jsonify({"status":"error","message":"Could not parse AI response. Try again."}),500
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}),500


# ── STRATEGY ENDPOINTS ───────────────────────────────────────────────

@app.route('/api/strategy/section8', methods=['POST'])
def strategy_section8():
    body       = request.json
    zipcode    = body.get("zip_code","").strip()
    rent       = float(body.get("monthly_rent",0))
    state      = body.get("state","NJ").upper()
    search_live= body.get("search_live",False)

    result={"strategy":"section8","footnotes":[]}
    safmr_data=NJ_SAFMR_2BR.get(zipcode)

    if safmr_data:
        safmr=safmr_data["safmr"]; area=safmr_data["area"]
        premium=safmr-rent; annual=premium*12
        result["footnotes"]+=[
            f"Source: NJ HUD 2026 SAFMR table (hardcoded, zero tokens).",
            f"Zip {zipcode} ({area}): 2-bedroom SAFMR = ${safmr:,}/mo.",
            f"Market rent used: ${rent:,.0f}/mo (from property search).",
            f"Monthly premium: ${safmr:,} - ${rent:,.0f} = ${premium:,.0f}.",
            f"Annual uplift: ${premium:,.0f} × 12 = ${annual:,.0f}.",
            "NJ Law Against Discrimination: landlords MUST accept Section 8 vouchers. $10K first violation.",
            "Process: LLC → HQS inspection → JCHA RFTA application → HAP payments (45-90 day lag)."
        ]
        col="green" if premium>=200 else ("yellow" if premium>=50 else "red")
        verdict="STRONG FIT" if col=="green" else ("MARGINAL" if col=="yellow" else "NOT VIABLE")
        result.update({"color":col,"verdict":verdict,
            "headline":f"SAFMR ${safmr:,}/mo vs market ${rent:,.0f}/mo → ${premium:,.0f}/mo premium",
            "numbers":{"safmr_rate":safmr,"market_rent":round(rent),
                       "monthly_premium":round(premium),"annual_uplift":round(annual),"area":area},
            "details":(["Strong premium justifies HQS compliance overhead","Budget 45-90 days for RFTA/inspection lag"] if col=="green"
                      else (["Small premium — evaluate HQS upgrade costs before committing"] if col=="yellow"
                      else ["Market rent at or above SAFMR — no upside from voucher program"]))})
    elif search_live and state!="NJ":
        try:
            r=client.messages.create(model="claude-sonnet-4-6",max_tokens=600,
                tools=[{"type":"web_search_20250305","name":"web_search"}],
                messages=[{"role":"user","content":f"Search HUD SAFMR 2-bedroom for zip {body.get('zip_code','')}, state {state}. Return ONLY JSON: {{\"safmr_2br\":<n>,\"area_name\":\"<n>\",\"source\":\"<url>\",\"year\":<n>}}"}])
            raw="".join(b.text for b in r.content if hasattr(b,"text"))
            hud=json.loads(raw[raw.find("{"):raw.rfind("}")+1])
            safmr=float(hud.get("safmr_2br",0))
            if safmr>0:
                premium=safmr-rent; annual=premium*12
                col="green" if premium>=200 else ("yellow" if premium>=50 else "red")
                result.update({"color":col,"verdict":"STRONG FIT" if col=="green" else ("MARGINAL" if col=="yellow" else "NOT VIABLE"),
                    "headline":f"SAFMR ${safmr:,.0f}/mo vs market ${rent:,.0f}/mo",
                    "numbers":{"safmr_rate":safmr,"market_rent":round(rent),"monthly_premium":round(premium),"annual_uplift":round(annual),"area":hud.get("area_name","")},
                    "footnotes":[f"Live HUD search: {hud.get('source','')} ({hud.get('year','')})."]})
            else:
                result.update({"color":"red","verdict":"NO DATA","headline":"SAFMR not found for this zip","details":[]})
        except Exception as e:
            result.update({"color":"red","verdict":"SEARCH FAILED","headline":str(e),"details":[]})
    else:
        result.update({"color":"red","verdict":"NOT IN TABLE",
            "headline":f"Zip {zipcode or 'unknown'} not in NJ SAFMR table",
            "details":["Enable 'Search HUD Live' toggle for out-of-state properties.","NJ table covers Hudson, Union, Middlesex counties."],
            "footnotes":["NJ SAFMR table covers 15 key zips. Toggle live HUD search for other states (~1K tokens)."]})
    return jsonify(result)


@app.route('/api/strategy/str', methods=['POST'])
def strategy_str():
    body=request.json
    zipcode=body.get("zip_code","")
    address=body.get("address","").lower()
    rent=float(body.get("monthly_rent",0))
    w2=body.get("w2_income",None)

    is_pocono=(zipcode in POCONO_ZIPS or
               any(x in address for x in ["tobyhanna","pocono","stroudsburg","mount pocono"]))

    # STR depreciation estimate
    price=float(body.get("purchase_price",0))
    sqft=int(body.get("sqft",0) or 1200)
    # Rough depreciation: structure 80% of price over 27.5 yrs
    annual_depreciation = (price * 0.80) / 27.5 if price > 0 else 0
    # Cost seg bonus: accelerated on 15-20% of value in yr1
    cost_seg_low  = price * 0.15 / 1  # 15% accelerated
    cost_seg_high = price * 0.20 / 1  # 20% accelerated
    total_dep_low  = annual_depreciation + cost_seg_low
    total_dep_high = annual_depreciation + cost_seg_high

    tax_section = {
        "annual_straight_line": round(annual_depreciation),
        "cost_seg_yr1_low": round(cost_seg_low),
        "cost_seg_yr1_high": round(cost_seg_high),
        "total_dep_yr1_low": round(total_dep_low),
        "total_dep_yr1_high": round(total_dep_high),
        "w2_offset": None, "estimated_refund_low": None, "estimated_refund_high": None,
        "footnotes": [
            f"Straight-line depreciation: ${price:,.0f} × 80% structure ÷ 27.5 yrs = ${annual_depreciation:,.0f}/yr.",
            f"Cost seg accelerated (Yr 1): 15-20% of value = ${cost_seg_low:,.0f}–${cost_seg_high:,.0f}.",
            f"Total estimated Yr 1 paper loss: ${total_dep_low:,.0f}–${total_dep_high:,.0f}.",
            "⚠ Cost segregation must be performed by a licensed cost seg professional.",
            "⚠ IRS scrutiny risk if done incorrectly. Consult a CPA before filing.",
            "STR tax loophole requires: avg stay < 7 days AND material participation (>100 hrs/yr, most of anyone)."
        ]
    }
    if w2 and w2 > 0:
        bracket = 0.32 if w2 > 170050 else (0.24 if w2 > 89075 else 0.22)
        refund_low  = total_dep_low * bracket
        refund_high = total_dep_high * bracket
        tax_section["w2_offset"] = round(w2)
        tax_section["bracket"]   = round(bracket*100)
        tax_section["estimated_refund_low"]  = round(refund_low)
        tax_section["estimated_refund_high"] = round(refund_high)
        tax_section["footnotes"].append(
            f"W2 income ${w2:,.0f} → estimated bracket {round(bracket*100)}%.")
        tax_section["footnotes"].append(
            f"Estimated tax refund: ${total_dep_low:,.0f}–${total_dep_high:,.0f} × {round(bracket*100)}% = ${refund_low:,.0f}–${refund_high:,.0f}.")

    checklist_base=[
        {"item":"AirDNA comp analysis","note":"Check ADR, occupancy %, monthly revenue for this zip","status":"action"},
        {"item":"Local STR permit","note":"Verify township/county permit requirements and cost","status":"action"},
        {"item":"Platform fees","note":"Airbnb 3% host + 14-16% guest fee affects pricing","status":"action"},
        {"item":"Cleaning cost model","note":"Net monthly must exceed LTR by 30%+ to justify active mgmt","status":"action"},
        {"item":"Seasonality","note":"Check AirDNA occupancy dips — some markets are 60-day seasons","status":"action"},
        {"item":"HOA / Condo rules","note":"Many NJ condos prohibit STR — verify CC&Rs","status":"action"},
    ]

    if is_pocono:
        return jsonify({"strategy":"str","color":"red","verdict":"NOT RECOMMENDED",
            "headline":"Pocono = one-night market. Cleaning costs kill NOI.",
            "checklist":[
                {"item":"Avg stay length","note":"Tri-state drive-in: arrive Sat, leave Sun","status":"fail"},
                {"item":"Cleaning cost ratio","note":"$150-200 clean ÷ $250-350/night = 43-80% cost ratio","status":"fail"},
                {"item":"LTR status","note":"Tobyhanna converted to LTR — correct strategy in place","status":"pass"},
            ],
            "tax_section":tax_section,
            "formula":"(ADR × Occ Days) - (Clean × Turnovers) - Platform Fees - Utilities = Net STR",
            "footnotes":["Pocono STR economics: one-night average stay makes cleaning cost unsustainable.",
                         "Tobyhanna (8277 Natures Drive) already converted from STR to LTR for this reason."]})
    else:
        return jsonify({"strategy":"str","color":"yellow","verdict":"VERIFY MANUALLY",
            "headline":"Run AirDNA before deciding — STR viable only if net > LTR by 30%+",
            "checklist":checklist_base,
            "tax_section":tax_section,
            "formula":"(ADR × Occ Days) - (Clean × Turnovers) - Platform Fees - Utilities = Net STR",
            "footnotes":["STR not auto-scored — manual AirDNA verification avoids token waste on non-vacation markets."]})


@app.route('/api/strategy/ltr', methods=['POST'])
def strategy_ltr():
    body=request.json
    price=float(body.get("purchase_price",0))
    rent=float(body.get("monthly_rent",0))
    taxes=float(body.get("annual_taxes",0))
    insurance=float(body.get("annual_insurance",0))
    water=float(body.get("water_sewer",0))
    year_built=int(body.get("year_built",0))
    dom=int(body.get("days_on_market",0))
    zipcode=body.get("zip_code","")
    down_pct=float(body.get("down_pct",25))
    rate=float(body.get("rate",6.5))
    loan_type=body.get("loan_type","investment")

    # ── BEFORE: current state at as-is rent ─────────────────────────
    f_before = calc_financials(price,rent,taxes,insurance,water,down_pct,rate,loan_type,BASE_CONFIG)
    noi_before     = f_before['noi']
    cap_before     = (noi_before/price)*100 if price>0 else 0
    cf_before      = f_before['annual_cf']

    reno_budget = price*(BASE_CONFIG['heloc_reno_pct']/100)
    prop_age    = 2026-year_built if year_built>1900 else 0
    safmr_data  = NJ_SAFMR_2BR.get(zipcode)

    # ── AFTER: model the actual rent bump a cosmetic reno produces ──
    # Industry standard: light cosmetic reno (paint/flooring/fixtures/lighting)
    # typically supports an 8-12% rent premium over as-is comps. Use 10% (mid).
    RENO_RENT_LIFT_PCT = 10
    rent_after = rent * (1 + RENO_RENT_LIFT_PCT/100)
    f_after = calc_financials(price,rent_after,taxes,insurance,water,down_pct,rate,loan_type,BASE_CONFIG)
    noi_after  = f_after['noi']
    cap_after  = (noi_after/price)*100 if price>0 else 0
    cf_after   = f_after['annual_cf']

    reno_payback = reno_budget/(noi_after-noi_before) if (noi_after-noi_before)>0 else 999
    fixes_cash_flow = cf_before < 0 and cf_after >= 0
    improves_materially = (cf_after - cf_before) > 1000  # meaningful annual improvement

    signals=[]
    if prop_age>=30: signals.append({"item":f"Property age: {prop_age} yrs","note":"Dated finishes = cosmetic upside","status":"pass"})
    if dom>30: signals.append({"item":f"{dom} days on market","note":"Extended DOM = negotiation room","status":"pass"})
    if 0<year_built<1990: signals.append({"item":f"Built {year_built}","note":"Pre-1990 = flooring/paint/fixture opportunity","status":"pass"})
    signals.append({"item":f"As-is: ${rent:,.0f}/mo → NOI ${noi_before:,.0f}/yr","note":f"Cap rate {cap_before:.2f}% · Cash flow ${cf_before:,.0f}/yr","status":"pass" if cf_before>=0 else "fail"})
    signals.append({"item":f"Post-reno: ${rent_after:,.0f}/mo → NOI ${noi_after:,.0f}/yr","note":f"Cap rate {cap_after:.2f}% · Cash flow ${cf_after:,.0f}/yr","status":"pass" if cf_after>=0 else "fail"})
    signals.append({"item":f"Reno budget: ${reno_budget:,.0f}","note":"7% of purchase price (cosmetic refresh: paint, flooring, fixtures)","status":"info"})
    signals.append({"item":f"Payback on INCREMENTAL NOI: {reno_payback:.1f} yrs" if reno_payback<900 else "Reno produces no NOI improvement","note":"Time to recoup reno cost from the rent increase ALONE (not total NOI)","status":"pass" if reno_payback<7 else ("warn" if reno_payback<12 else "fail")})
    if cf_before<0 and cf_after<0:
        signals.append({"item":"⚠ Reno does not fix the core problem","note":f"Still ${cf_after:,.0f}/yr negative cash flow even after a 10% rent bump — purchase price is the issue, not the property condition","status":"fail"})
    if safmr_data:
        signals.append({"item":"Section 8 available","note":f"SAFMR ${safmr_data['safmr']:,}/mo in {zipcode} — may exceed post-reno market rent","status":"info"})

    # ── Verdict logic: only STRONG FIT if reno actually fixes the deal ──
    if fixes_cash_flow and reno_payback<7:
        col,verdict="green","STRONG FIT"
        headline=f"Reno fixes cash flow: ${cf_before:,.0f} → ${cf_after:,.0f}/yr, payback {reno_payback:.1f}yrs"
    elif improves_materially and reno_payback<10:
        col,verdict="yellow","PARTIAL FIX"
        headline=f"Reno improves but doesn't fully fix: ${cf_before:,.0f} → ${cf_after:,.0f}/yr — still {'negative' if cf_after<0 else 'thin'}"
    else:
        col,verdict="red","DOES NOT FIX DEAL"
        headline=f"Even with reno: ${cf_after:,.0f}/yr cash flow — purchase price is too high relative to rent, not a condition problem"

    return jsonify({"strategy":"ltr","color":col,"verdict":verdict,"headline":headline,
        "numbers":{"rent_before":round(rent),"rent_after":round(rent_after),
                   "noi_before":round(noi_before),"noi_after":round(noi_after),
                   "cap_rate_before":round(cap_before,2),"cap_rate_after":round(cap_after,2),
                   "cash_flow_before":round(cf_before),"cash_flow_after":round(cf_after),
                   "reno_budget":round(reno_budget),
                   "reno_payback_years":round(reno_payback,1) if reno_payback<900 else None},
        "checklist":signals,
        "footnotes":[
            f"Step 1 — As-is rent: ${rent:,.0f}/mo → Annual gross ${rent*12:,.0f} → NOI ${noi_before:,.0f} → Cap rate {cap_before:.2f}%.",
            f"Step 2 — As-is cash flow: ${cf_before:,.0f}/yr ({'NEGATIVE — property loses money today' if cf_before<0 else 'positive'}).",
            f"Step 3 — Modeled rent lift: cosmetic reno (paint/flooring/fixtures) supports a {RENO_RENT_LIFT_PCT}% rent premium over as-is comps (industry standard for light renovation).",
            f"Step 4 — Post-reno rent: ${rent:,.0f} × 1.{RENO_RENT_LIFT_PCT} = ${rent_after:,.0f}/mo.",
            f"Step 5 — Post-reno NOI: ${noi_after:,.0f} (vs ${noi_before:,.0f} before) → Cap rate {cap_after:.2f}%.",
            f"Step 6 — Post-reno cash flow: ${cf_after:,.0f}/yr ({'still negative' if cf_after<0 else 'POSITIVE — reno fixes the deal'}).",
            f"Step 7 — Reno budget: {BASE_CONFIG['heloc_reno_pct']}% × ${price:,.0f} = ${reno_budget:,.0f} (cosmetic only — paint, flooring, fixtures, lighting).",
            f"Step 8 — Payback: ${reno_budget:,.0f} reno cost ÷ (${noi_after:,.0f} - ${noi_before:,.0f} incremental NOI) = {reno_payback:.1f} years." if reno_payback<900 else "Step 8 — Reno produces no incremental NOI — payback undefined.",
            "⚠ This models a TYPICAL cosmetic renovation. Actual rent lift depends on scope, comps, and execution — verify with a contractor estimate and updated comps before committing capital.",
            "B-in-A play (separate from this calc): Neighborhood score ≥ 7.5, Property score ≤ 4, Gap ≥ 3 points — requires physical tour, not estimated here."
        ]})


@app.route('/api/strategy/flip', methods=['POST'])
def strategy_flip():
    body=request.json
    price    =float(body.get("purchase_price",0))
    taxes    =float(body.get("annual_taxes",0))
    insurance=float(body.get("annual_insurance",0))
    water    =float(body.get("water_sewer",0))
    sqft     =int(body.get("sqft",0))
    state    =body.get("state","NJ").upper()
    rent     =float(body.get("monthly_rent",0))
    est_value=float(body.get("estimated_current_value",0))
    address  =body.get("address","")
    down_pct =float(body.get("down_pct",10))
    rate     =float(body.get("rate",7.0))
    loan_type=body.get("loan_type","investment")

    # Live construction cost search
    try:
        r=client.messages.create(model="claude-sonnet-4-6",max_tokens=600,
            tools=[{"type":"web_search_20250305","name":"web_search"}],
            messages=[{"role":"user","content":f"Current residential full gut renovation cost per sqft near: {address} ({state}). Search HomeAdvisor, Angi, RSMeans, local contractors 2025-2026. Return ONLY JSON: {{\"cost_per_sqft\":<n>,\"scope\":\"<scope>\",\"source\":\"<src>\",\"date_of_data\":\"<date>\"}}"}])
        raw="".join(b.text for b in r.content if hasattr(b,"text"))
        cc=json.loads(raw[raw.find("{"):raw.rfind("}")+1])
        searched_cost=float(cc.get("cost_per_sqft",0))
        cost_source=cc.get("source","unknown"); cost_date=cc.get("date_of_data","unknown")
        cost_scope=cc.get("scope","full gut renovation")
    except:
        searched_cost=0; cost_source="search failed"; cost_date="unknown"; cost_scope="unknown"

    floor=BASE_CONFIG['nj_construction_floor'] if state in ("NJ","NY","CT") else BASE_CONFIG['pa_construction_floor']
    cost_psf=max(searched_cost,floor)
    eff_sqft=sqft if sqft>0 else 1200

    def flip_scenario(ltype, dpct, lrate, label):
        f=calc_financials(price,rent,taxes,insurance,water,dpct,lrate,ltype,BASE_CONFIG)
        piti=f['mortgage']+(taxes/12)+(insurance/12)
        construction=cost_psf*eff_sqft
        carrying=piti*BASE_CONFIG['flip_carry_months']
        closing_sell=price*0.06
        total_cost=price+construction+carrying+closing_sell
        arv_needed=total_cost/(1-BASE_CONFIG['flip_margin_floor']/100)
        arv_est=est_value*1.15 if est_value>0 else price*1.30
        margin=((arv_est-total_cost)/arv_est)*100 if arv_est>0 else 0
        col="green" if margin>=BASE_CONFIG['flip_margin_floor'] else ("yellow" if margin>=10 else "red")
        return {
            "label":label,"color":col,
            "verdict":"VIABLE" if col=="green" else ("THIN" if col=="yellow" else "NOT VIABLE"),
            "margin_pct":round(margin,1),
            "numbers":{
                "loan_type":label,"down_pct":dpct,"rate":f"{lrate}%",
                "construction":round(construction),"carrying":round(carrying),
                "selling_costs":round(closing_sell),"total_all_in":round(total_cost),
                "estimated_arv":round(arv_est),"arv_needed":round(arv_needed),
                "net_margin":f"{margin:.1f}%"
            },
            "footnotes":[
                f"Loan: {label} — {dpct}% down, {lrate}% rate.",
                f"Construction: ${cost_psf}/sqft × {eff_sqft} sqft = ${construction:,.0f} (source: {cost_source}, {cost_date}).",
                f"State floor: ${floor}/sqft ({'NJ/NY/CT' if state in ('NJ','NY','CT') else 'PA/other'}). Used: ${cost_psf}/sqft.",
                f"Monthly PITI: ${f['mortgage']:,.0f} + ${taxes/12:,.0f} + ${insurance/12:,.0f} = ${piti:,.0f}.",
                f"Carrying ({BASE_CONFIG['flip_carry_months']} mo): ${piti:,.0f} × 9 = ${carrying:,.0f}.",
                f"Selling (6%): ${closing_sell:,.0f}.",
                f"Total all-in: ${price:,.0f} + ${construction:,.0f} + ${carrying:,.0f} + ${closing_sell:,.0f} = ${total_cost:,.0f}.",
                f"ARV estimate: ${arv_est:,.0f} (current value × 1.15).",
                f"Margin: (${arv_est:,.0f} - ${total_cost:,.0f}) / ${arv_est:,.0f} = {margin:.1f}%.",
                f"Required ARV for {BASE_CONFIG['flip_margin_floor']}% floor: ${arv_needed:,.0f}.",
            ]
        }

    scenario_a=flip_scenario(loan_type,down_pct,rate,LOAN_DEFAULTS.get(loan_type,{}).get('label','Your Loan'))
    scenario_b=flip_scenario('hard_money',15,12.0,'Hard Money (Fast Close)')
    best_col=scenario_a['color'] if scenario_a['margin_pct']>=scenario_b['margin_pct'] else scenario_b['color']

    return jsonify({"strategy":"flip","color":best_col,
        "verdict":f"A: {scenario_a['verdict']} | B: {scenario_b['verdict']}",
        "headline":f"Scenario A: {scenario_a['margin_pct']}% margin | Scenario B: {scenario_b['margin_pct']}% margin",
        "scenario_a":scenario_a,"scenario_b":scenario_b,
        "footnotes":["Flip requires permits, contractor vetting, draw schedule, and municipality timeline verification.",
                     "Hard Money closes in 7 days but 12% rate increases carrying cost significantly.",
                     "If Scenario B still shows 20%+ margin, deal can absorb fast-money premium."]})


@app.route('/api/strategy/heloc', methods=['POST'])
def strategy_heloc():
    body     =request.json
    price    =float(body.get("purchase_price",0))
    rent     =float(body.get("monthly_rent",0))
    taxes    =float(body.get("annual_taxes",0))
    insurance=float(body.get("annual_insurance",0))
    water    =float(body.get("water_sewer",0))
    est_value=float(body.get("estimated_current_value",0))
    down_pct =float(body.get("down_pct",10))
    rate     =float(body.get("rate",7.0))
    loan_type=body.get("loan_type","investment")

    f=calc_financials(price,rent,taxes,insurance,water,down_pct,rate,loan_type,BASE_CONFIG)
    loan_balance=f['loan_amt']

    if est_value<=0:
        return jsonify({"strategy":"heloc","color":"red","verdict":"NO DATA",
            "headline":"Estimated current value not found — cannot calculate equity","footnotes":[]})

    equity    =est_value-loan_balance
    equity_pct=(equity/est_value)*100
    max_heloc =(est_value*0.75)-loan_balance
    reno_cost =price*(BASE_CONFIG['heloc_reno_pct']/100)
    arv_boost =reno_cost*BASE_CONFIG['heloc_arv_multiplier']
    proj_arv  =est_value+arv_boost
    double_play=arv_boost>reno_cost

    col="green" if equity_pct>=BASE_CONFIG['heloc_equity_threshold'] else ("yellow" if equity_pct>=15 else "red")
    if col=="green" and not double_play: col="yellow"
    verdict=("RECOMMENDED — DOUBLE PLAY" if col=="green" and double_play
             else "VIABLE" if equity_pct>=BASE_CONFIG['heloc_equity_threshold']
             else "MARGINAL" if equity_pct>=15 else "NOT VIABLE")

    return jsonify({"strategy":"heloc","color":col,"verdict":verdict,
        "headline":(f"DOUBLE PLAY: {equity_pct:.0f}% equity + reno = {BASE_CONFIG['heloc_arv_multiplier']}x ARV lift"
                   if double_play and col=="green" else f"{equity_pct:.0f}% equity position"),
        "numbers":{
            "estimated_value":est_value,
            "original_purchase":price,
            "down_payment_at_purchase":round(f['down']),
            "loan_balance_estimate":round(loan_balance),
            "equity_dollars":round(equity),
            "equity_pct":round(equity_pct,1),
            "max_heloc_draw":round(max_heloc),
            "reno_budget":round(reno_cost),
            "arv_boost":round(arv_boost),
            "projected_arv":round(proj_arv),
            "double_play":double_play
        },
        "footnotes":[
            f"Step 1 — Estimated current value: ${est_value:,.0f} (Zillow/Redfin estimate from property search).",
            f"Step 2 — Loan balance estimate: ${price:,.0f} purchase - ${f['down']:,.0f} down ({down_pct}%) = ${loan_balance:,.0f}.",
            f"Step 3 — Equity: ${est_value:,.0f} - ${loan_balance:,.0f} = ${equity:,.0f}.",
            f"Step 4 — Equity %: ${equity:,.0f} / ${est_value:,.0f} = {equity_pct:.1f}%.",
            f"Step 5 — Threshold check: {equity_pct:.1f}% {'≥' if equity_pct>=BASE_CONFIG['heloc_equity_threshold'] else '<'} {BASE_CONFIG['heloc_equity_threshold']}% minimum → {'PASSES' if equity_pct>=BASE_CONFIG['heloc_equity_threshold'] else 'FAILS'}.",
            f"Step 6 — Max HELOC at 75% LTV: (${est_value:,.0f} × 0.75) - ${loan_balance:,.0f} = ${max_heloc:,.0f}.",
            f"Step 7 — Cosmetic reno budget: {BASE_CONFIG['heloc_reno_pct']}% × ${price:,.0f} = ${reno_cost:,.0f}.",
            f"Step 8 — ARV boost test: ${reno_cost:,.0f} × {BASE_CONFIG['heloc_arv_multiplier']}x = ${arv_boost:,.0f} required value increase.",
            f"Step 9 — Projected post-reno ARV: ${est_value:,.0f} + ${arv_boost:,.0f} = ${proj_arv:,.0f}.",
            f"Step 10 — Double play: {'YES — reno produces ' + str(BASE_CONFIG['heloc_arv_multiplier']) + 'x ARV lift' if double_play else 'NO — ARV lift insufficient for double play'}.",
            "⚠ Loan balance is ESTIMATED based on original purchase price minus down payment. Request actual payoff statement from lender before proceeding."
        ]})


if __name__=='__main__':
    app.run(port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
