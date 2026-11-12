import base64
from email.mime.text import MIMEText
from gmail_listings import get_gmail_service
from pipeline import load_results, build_digest

def send_digest():
    results = load_results()
    if not results:
        print("No analyzed listings to send.")
        return
    service = get_gmail_service()
    me = service.users().getProfile(userId='me').execute()['emailAddress']
    msg = MIMEText(build_digest(results))
    msg['To'] = me
    msg['From'] = me
    msg['Subject'] = f"Deal Scanner Digest: {len(results)} listing(s)"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId='me', body={'raw': raw}).execute()
    print(f"Digest sent to {me}")

if __name__ == '__main__':
    send_digest()
