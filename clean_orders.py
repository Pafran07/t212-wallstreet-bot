import os, requests
from dotenv import load_dotenv

load_dotenv('C:/Users/pafra/.gemini/antigravity/scratch/trading212/.env')
auth = (os.getenv('T212_API_KEY'), os.getenv('T212_SECRET_KEY'))
orders = requests.get('https://live.trading212.com/api/v0/equity/orders', auth=auth).json()
print('Total open orders:', len(orders))
for i, o in enumerate(orders):
    oid = o['id']
    ticker = o['ticker']
    qty = o['quantity']
    print(f"{i}: {oid} {ticker} qty={qty}")
    if i > 0:
        r = requests.delete(f"https://live.trading212.com/api/v0/equity/orders/{oid}", auth=auth)
        print(f"Cancelled {oid}: HTTP {r.status_code}")

# Check cash now
c = requests.get('https://live.trading212.com/api/v0/equity/account/cash', auth=auth).json()
print("Cash status:", c)
