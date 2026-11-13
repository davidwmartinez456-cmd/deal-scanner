# Changelog

Newest first. Each entry says what changed and what to do differently.

## 2026-11-13
- Added this changelog.
- Daily Startup Guide rebuilt for the new folder and file names. A copy now lives in this folder and is excluded from GitHub.
- **Do differently:** nothing. Commands are unchanged from Nov 11.

## 2026-11-11
- Added the email pipeline: `pipeline.py` scores each new listing once, `send_digest.py` emails the ranked digest.
- Gmail reader now captures unit count; multi-family listings are scored on total rent.
- Server answers only its own page: binds to `127.0.0.1`, serves the frontend itself, refuses other web origins.
- Scan results (`pipeline_results*.json`) excluded from GitHub.
- **Do differently:** open the scanner at `http://127.0.0.1:5000`. Opening `index.html` directly no longer works.

## 2026-11-10
- Project prepared for publishing in its own folder, `deal-scanner-github`.
- Files renamed: `secure_deal_scanner_server.py` to `server.py`, `secure_deal_scanner_frontend.html` to `index.html`, `gmail_skill.py` to `gmail_listings.py`.
- Debug mode off by default. Set `FLASK_DEBUG=1` in `.env` to turn it on.
- Added `README.md`, `requirements.txt`, `.env.example`, `.gitignore`.
- **Do differently:** start the server with `cd ~/Desktop/"LLM tools"/deal-scanner-github && python3 server.py`. Restart it by hand after editing code.
