import os
import base64
import re
from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_gmail_service():
    creds = None
    token_path = os.path.join(BASE_DIR, 'token.json')
    creds_path = os.path.join(BASE_DIR, 'credentials.json')
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def get_email_html(msg):
    payload = msg['payload']
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/html':
                data = part['body'].get('data', '')
                return base64.urlsafe_b64decode(data).decode('utf-8')
            if 'parts' in part:
                for subpart in part['parts']:
                    if subpart['mimeType'] == 'text/html':
                        data = subpart['body'].get('data', '')
                        return base64.urlsafe_b64decode(data).decode('utf-8')
    if 'body' in payload:
        data = payload['body'].get('data', '')
        return base64.urlsafe_b64decode(data).decode('utf-8')
    return ''

def parse_listings_from_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    lines = [l.strip() for l in soup.get_text(separator='\n').split('\n') if l.strip()]
    
    listings = []
    i = 0
    while i < len(lines):
        # Look for price line
        price_match = re.match(r'^\$([0-9,]+)$', lines[i])
        if price_match:
            price = int(price_match.group(1).replace(',', ''))
            # Next lines: property type, family type, address, city, state/zip
            if i + 4 < len(lines):
                address_line = lines[i + 3]
                city_line = lines[i + 4]
                state_line = lines[i + 5] if i + 5 < len(lines) else ''
                state_match = re.match(r'^(NJ)\s+(\d{5})', state_line)
                if state_match:
                    full_address = f"{address_line}, {city_line}, NJ {state_match.group(2)}"
                    listings.append({
                        'address': full_address,
                        'price': price
                    })
        i += 1
    return listings

def get_rapattoni_emails(max_results=5):
    service = get_gmail_service()
    results = service.users().messages().list(
        userId='me',
        q='from:MLSAutoprospecting@rapattoni.com',
        maxResults=max_results
    ).execute()
    messages = results.get('messages', [])
    emails = []
    for msg in messages:
        message = service.users().messages().get(
            userId='me', id=msg['id'], format='full'
        ).execute()
        html = get_email_html(message)
        listings = parse_listings_from_html(html)
        if listings:
            emails.append({'email_id': msg['id'], 'listings': listings})
    return emails

if __name__ == '__main__':
    print("Testing Gmail connection...")
    emails = get_rapattoni_emails(max_results=3)
    if emails:
        print(f"Found {len(emails)} email(s) with listings")
        for email in emails:
            for listing in email['listings']:
                print(f"  Address: {listing['address']}")
                print(f"  Price:   ${listing['price']:,}")
                print()
    else:
        print("No listings parsed")
