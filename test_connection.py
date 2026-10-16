import os, requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("T212_API_KEY", "").strip()
secret_key = os.getenv("T212_SECRET_KEY", "").strip()

if not api_key or not secret_key:
    print("❌ Falta rellenar T212_API_KEY y T212_SECRET_KEY en el archivo .env")
    exit(1)

auth = (api_key, secret_key)
base_url = "https://live.trading212.com/api/v0"

try:
    # 1. Info de cuenta
    res = requests.get(f"{base_url}/equity/account/info", auth=auth, timeout=10)
    if res.status_code == 200:
        info = res.json()
        print("✅ Conexión con Trading 212 establecida con éxito!")
        print(f"🆔 ID de cuenta: {info.get('id')}")
        print(f"💶 Moneda: {info.get('currencyCode')}")
    else:
        print(f"ℹ️ Status Info: {res.status_code}")

    # 2. Balance de efectivo
    cash_res = requests.get(f"{base_url}/equity/account/cash", auth=auth, timeout=10)
    if cash_res.status_code == 200:
        cash = cash_res.json()
        print(f"💰 Saldo Libre: {cash.get('free', 0):.2f} € | Total: {cash.get('total', 0):.2f} €")
    
    # 3. Posiciones abiertas
    pos_res = requests.get(f"{base_url}/equity/portfolio", auth=auth, timeout=10)
    if pos_res.status_code == 200:
        positions = pos_res.json()
        print(f"📂 Posiciones abiertas: {len(positions)}")
        for p in positions:
            print(f"   • {p.get('ticker')}: {p.get('quantity')} acciones | PnL: {p.get('ppl', 0):.2f} €")

except Exception as e:
    print(f"❌ Error de conexión: {e}")
