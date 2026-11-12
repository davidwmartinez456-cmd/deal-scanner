import json, os, re, sys
import requests
from gmail_listings import get_rapattoni_emails

API = 'http://127.0.0.1:5000/api/analyze'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(BASE_DIR, 'pipeline_results.json')

def clean(addr):
    # "52 Randolph St St" -> "52 Randolph St"
    return re.sub(r'\b(\w+)(\s+\1\b)+', r'\1', addr, flags=re.IGNORECASE)

def load_results():
    if not os.path.exists(RESULTS):
        return {}
    with open(RESULTS) as f:
        raw = json.load(f)
    fixed = {}
    for k, v in raw.items():
        v['listing']['address'] = clean(v['listing']['address'])
        fixed[clean(k).lower()] = v
    return fixed

def save_results(results):
    with open(RESULTS, 'w') as f:
        json.dump(results, f, indent=2)

def collect_listings():
    seen = {}
    for email in get_rapattoni_emails(max_results=5):
        for l in email['listings']:
            l['address'] = clean(l['address'])
            key = l['address'].lower()
            if key not in seen:
                seen[key] = l
    return list(seen.values())

def analyze(listing):
    r = requests.post(API, json={
        'address': listing['address'],
        'price_override': listing['price'],
        'tier': 'basic',
        'units': listing.get('units', 1)
    }, timeout=300)
    try:
        return r.json()
    except ValueError:
        return {'status': 'error', 'message': f'HTTP {r.status_code}: {r.text[:300]}'}

def money(n):
    n = n or 0
    return f"-${abs(n):,.0f}" if n < 0 else f"${n:,.0f}"

def score_num(r):
    try:
        return int(str(r['analysis'].get('score', '0/8')).split('/')[0])
    except ValueError:
        return 0

def build_digest(results):
    rows = sorted(results.values(), key=score_num, reverse=True)
    out = [f"DEAL SCANNER DIGEST: {len(rows)} listing(s)", ""]
    for r in rows:
        a, l = r['analysis'], r['listing']
        d = a.get('derived', {})
        n = a.get('negotiation_target', {})
        fails = [s['metric'] for s in a.get('scorecard', []) if not s.get('pass')]
        out += [
            f"{a.get('decision')}  {a.get('score')}  {l['address']}",
            f"  Ask {money(l['price'])} | Cap {d.get('cap_rate')}% | CoC {d.get('coc')}% | DSCR {d.get('dscr')} | CF {money(d.get('monthly_cf'))}/mo",
            f"  Max price {money(n.get('ceiling'))} ({n.get('binding_constraint')} binds) | Opening offer {money(n.get('opening_offer'))}",
            f"  Fails: {', '.join(fails) or 'none'}",
            f"  Rent used {money(a.get('inputs', {}).get('monthly_rent'))}/mo for {l.get('units', 1)} unit(s), AI estimate",
            f"  {a.get('verdict')}",
            ""
        ]
    return "\n".join(out)

if __name__ == '__main__':
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    results = load_results()

    if limit > 0:
        listings = collect_listings()
        todo = [l for l in listings if l['address'].lower() not in results]
        print(f"{len(listings)} unique listings, {len(todo)} not yet analyzed, running {min(limit, len(todo))}")
        for l in todo[:limit]:
            print(f"Analyzing: {l['address']} ({money(l['price'])}) ...")
            try:
                data = analyze(l)
            except requests.exceptions.ConnectionError:
                print("Server not reachable. Start it in the server terminal first.")
                sys.exit(1)
            if data.get('status') == 'error':
                print(f"  ERROR: {data.get('message')}")
                continue
            results[l['address'].lower()] = {'listing': l, 'analysis': data}
            save_results(results)
            print(f"  {data.get('decision')} {data.get('score')}")

    save_results(results)
    print()
    print(build_digest(results))
