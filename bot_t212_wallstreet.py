#!/usr/bin/env python3
"""
bot_t212_wallstreet.py — Bot Cuantitativo Multi-Sector Momentum para Trading 212
================================================================================
Estrategia v4.0 (Trend & Momentum Following):
  - Universo Ampliado: ~50 tickers (Tech, Cripto, Metales, Petróleo, Consumo, Salud).
  - Lógica de Entrada: "Comprar lo que está subiendo con fuerza institucional"
      1. Tendencia Alcista: Precio > EMA(20) y Precio > EMA(50).
      2. Momentum Saludable: 50 <= RSI(14) <= 68 (fuerza compradora sin sobrecompra extrema).
      3. Giro/Continuación Alcista: Vela actual verde (Close >= Open/Close anterior).
      4. Volumen Confirmado: Volumen de la vela >= media móvil de volumen (20 velas).
  - Gestión de Riesgo y Salida:
      - Trailing Stop Dinámico: Se activa a partir de +1.2%, con callback de 0.6% desde el pico.
      - Hard Stop Loss: -1.2% estricto de protección de capital.
      - Breakeven Lock: Si ya tocó +0.8%, no permite caer en negativo.
"""

import os
import sys
import time
import json
import logging
import argparse
import pathlib
from datetime import datetime

from dotenv import load_dotenv
import requests

# ── Codificación ──────────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Configuración ─────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY      = os.getenv("T212_API_KEY", "").strip()
SECRET_KEY   = os.getenv("T212_SECRET_KEY", "").strip()
AUTH         = (API_KEY, SECRET_KEY)
BASE_URL     = "https://live.trading212.com/api/v0"
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "").strip()

# ── Parámetros de estrategia Momentum v4.0 ────────────────────────────────────
RSI_PERIOD      = 14
RSI_MIN_MOM     = 50.0    # Fuerza alcista institucional
RSI_MAX_MOM     = 68.0    # Evitar entrar en sobrecompra extrema / techo
EMA_SHORT       = 20
EMA_LONG        = 50
VOLUME_MULT     = 0.95    # Volumen consistente

HARD_SL         = 0.012   # Stop Loss en -1.2%
TRAIL_ACTIVATE  = 0.012   # Trailing se arma en +1.20% (antes era +1.40%)
TRAIL_CALLBACK  = 0.0045  # Callback óptimo de 0.45% desde el pico (equilibrio antiruido)

LOOP_SECONDS    = 20
MAX_POSITIONS   = 4       # 4 slots para aprovechar el capital ampliado (~57€ con regalo NVDA)
NEWS_CACHE_MIN  = 15

# ── Universo de tickers ───────────────────────────────────────────────────────
UNIVERSE_PATH = pathlib.Path(__file__).with_name("universe.json")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "bot_t212.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("t212_wallstreet")

try:
    with UNIVERSE_PATH.open(encoding="utf-8") as f:
        UNIVERSE = json.load(f)
    log.info(f"✅ Universo cargado: {len(UNIVERSE)} activos listados.")
except Exception as e:
    log.warning(f"⚠️ Error cargando universe.json: {e}")
    UNIVERSE = {"NVDA": "NVDA_US_EQ", "WMT": "WMT_US_EQ", "NFLX": "NFLX_US_EQ"}

# ── API helpers ───────────────────────────────────────────────────────────────

def get_cash() -> dict:
    try:
        r = requests.get(f"{BASE_URL}/equity/account/cash", auth=AUTH, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning(f"Error consultando cash: {e}")
    return {}


def get_portfolio() -> list:
    try:
        r = requests.get(f"{BASE_URL}/equity/portfolio", auth=AUTH, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning(f"Error consultando portfolio: {e}")
    return []


def get_pending_orders() -> list:
    try:
        r = requests.get(f"{BASE_URL}/equity/orders", auth=AUTH, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning(f"Error consultando orders: {e}")
    return []


def calc_ema(prices: list, period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def get_market_data(yahoo_symbol: str) -> tuple:
    """
    Descarga velas de 5m de Yahoo Finance.
    Retorna: (precio, rsi, closes, vol_actual, vol_media_20, ema20, ema50)
    """
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
        "?interval=5m&range=5d"
    )
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=10)
    data = r.json()

    result = data["chart"]["result"][0]
    quotes = result["indicators"]["quote"][0]

    raw_closes  = quotes.get("close",  [])
    raw_volumes = quotes.get("volume", [])

    paired = [(c, v) for c, v in zip(raw_closes, raw_volumes) if c is not None and v is not None]
    if len(paired) < EMA_LONG + 5:
        return 0.0, 50.0, [], 0, 0, 0.0, 0.0

    closes  = [p[0] for p in paired]
    volumes = [p[1] for p in paired]

    # RSI(14)
    deltas   = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains    = [max(d, 0) for d in deltas]
    losses   = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
    avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
    for g, l in zip(gains[RSI_PERIOD:], losses[RSI_PERIOD:]):
        avg_gain = (avg_gain * (RSI_PERIOD - 1) + g) / RSI_PERIOD
        avg_loss = (avg_loss * (RSI_PERIOD - 1) + l) / RSI_PERIOD
    rs  = avg_gain / avg_loss if avg_loss > 0 else 100
    rsi = round(100 - 100 / (1 + rs), 2)

    ema20 = calc_ema(closes, EMA_SHORT)
    ema50 = calc_ema(closes, EMA_LONG)

    # Volumen consolidado: Yahoo suele dar 0 en la vela abierta actual; tomar la última con volumen > 0
    vol_actual = volumes[-1] if volumes[-1] > 0 else (volumes[-2] if len(volumes) >= 2 else 0)
    vol_media  = sum(volumes[-21:-1]) / min(20, len(volumes) - 1) if len(volumes) > 1 else 1.0

    return closes[-1], rsi, closes, vol_actual, vol_media, ema20, ema50


def place_order_market(ticker_t212: str, quantity: float, dry_run: bool) -> bool:
    if dry_run:
        log.info(f"[DRY-RUN] COMPRA MERCADO: {ticker_t212} qty={quantity:.4f}")
        return True
    body = {"quantity": round(quantity, 4), "ticker": ticker_t212}
    r = requests.post(f"{BASE_URL}/equity/orders/market", auth=AUTH, json=body, timeout=10)
    if r.status_code in (200, 201):
        log.info(f"💥 ORDEN DE COMPRA EJECUTADA: {ticker_t212} qty={quantity:.4f}")
        return True
    log.error(f"Error compra {ticker_t212}: HTTP {r.status_code} - {r.text}")
    return False


def sell_position_market(ticker_t212: str, quantity: float, reason: str, dry_run: bool) -> bool:
    if dry_run:
        log.info(f"[DRY-RUN] VENTA [{reason}]: {ticker_t212} qty={quantity:.4f}")
        return True
    body = {"quantity": -abs(round(quantity, 4)), "ticker": ticker_t212}
    r = requests.post(f"{BASE_URL}/equity/orders/market", auth=AUTH, json=body, timeout=10)
    if r.status_code in (200, 201):
        log.info(f"🟢 VENTA EJECUTADA [{reason}]: {ticker_t212} qty={quantity:.4f}")
        return True
    if "not-owned" in r.text or r.status_code == 400:
        log.warning(f"⚠️ {ticker_t212} cerrada externamente.")
        return True
    log.error(f"Error venta {ticker_t212}: HTTP {r.status_code} - {r.text}")
    return False


high_water_marks: dict = {}
trailing_active: dict  = {}


def run(dry_run: bool, max_minutes: int = 0):
    log.info("=" * 70)
    log.info("🚀 TRADING 212 — WALL STREET MOMENTUM & TREND ENGINE v4.2")
    log.info(f"   Universo ({len(UNIVERSE)} activos): Multi-sectorial (Metales, Energía, Consumo, Tech, Cripto)")
    log.info(f"   Estrategia    : Precio > EMA20 > EMA50 | {RSI_MIN_MOM} <= RSI <= {RSI_MAX_MOM} | Vela Alcista")
    log.info(f"   Stop Loss     : -{HARD_SL*100:.1f}%  | Trailing Stop: +{TRAIL_ACTIVATE*100:.1f}% (cb: -{TRAIL_CALLBACK*100:.1f}%)")
    if max_minutes > 0:
        log.info(f"   Duración máx. : {max_minutes} minutos (Modo Cloud / GitHub Actions)")
    log.info(f"   Modo          : {'DRY-RUN' if dry_run else 'TRADING REAL'}")
    log.info("=" * 70)

    start_ts = time.time()
    loop = 0
    while True:
        if max_minutes > 0 and (time.time() - start_ts) >= max_minutes * 60:
            log.info(f"⏹️ Turno de {max_minutes} minutos completado limpiamente. Cerrando proceso.")
            break
        loop += 1
        now_local = datetime.now()
        now_str   = now_local.strftime("%H:%M:%S")

        hora_min = now_local.hour * 60 + now_local.minute
        es_dia_laborable = now_local.weekday() < 5   # 0=Lunes ... 4=Viernes
        NYSE_OPEN        = 15 * 60 + 30   # 15:30 Madrid (Apertura mercado / Gestión salidas)
        NYSE_BUY_START   = 15 * 60 + 45   # 15:45 Madrid (Evitar volatilidad falsa de primeros 15m)
        NYSE_BUY_STOP    = 21 * 60 + 0    # 21:00 Madrid (No abrir compras a última hora)
        NYSE_FORCE_CLOSE = 21 * 60 + 50   # 21:50 Madrid (Cierre intradía preventivo)
        NYSE_CLOSE       = 22 * 60 + 0    # 22:00 Madrid

        mercado_abierto  = es_dia_laborable and (NYSE_OPEN <= hora_min < NYSE_CLOSE)
        ventana_compras  = es_dia_laborable and (NYSE_BUY_START <= hora_min < NYSE_BUY_STOP)

        cash         = get_cash()
        free_eur     = float(cash.get("free", 0))
        total_eur    = float(cash.get("total", free_eur))
        portfolio    = get_portfolio()
        orders       = get_pending_orders()

        # Separar órdenes: las SELL pendientes NO bloquean slots de entrada
        buy_pending_tkrs  = {o.get("ticker") for o in orders
                             if o.get("status") in ("NEW", "SUBMITTED", "PENDING")
                             and float(o.get("quantity", 0)) > 0}
        sell_pending_tkrs = {o.get("ticker") for o in orders
                             if o.get("status") in ("NEW", "SUBMITTED", "PENDING")
                             and float(o.get("quantity", 0)) < 0}

        held_tickers = {p.get("ticker") for p in portfolio} | buy_pending_tkrs
        active_count = len(portfolio) + len(buy_pending_tkrs)

        # ── REGLA INTRADÍA: Cierre total a las 21:50 para dormir 100% en liquidez ─
        if es_dia_laborable and (NYSE_FORCE_CLOSE <= hora_min < NYSE_CLOSE) and portfolio:
            log.warning("🌙 CIERRE INTRADÍA PREVENTIVO (21:50). Liquidando todo a mercado para evitar riesgo nocturno...")
            for pos in portfolio:
                t212_ticker = pos.get("ticker", "")
                if t212_ticker in sell_pending_tkrs:
                    continue
                qty = float(pos.get("quantity", 0))
                sell_position_market(t212_ticker, qty, "CIERRE_NOCTURNO", dry_run)
                high_water_marks.pop(t212_ticker, None)
                trailing_active.pop(t212_ticker, None)
            time.sleep(LOOP_SECONDS)
            continue

        log.info(
            f"\n--- [Ciclo #{loop} {now_str}] "
            f"Slots ocupados: {len(portfolio)}/{MAX_POSITIONS} | "
            f"{'🟢 MERCADO ABIERTO' if mercado_abierto else '🔴 MERCADO CERRADO'} ---"
        )

        if not mercado_abierto:
            for pos in portfolio:
                t212_ticker   = pos.get("ticker", "")
                qty           = float(pos.get("quantity", 0))
                avg_price     = float(pos.get("averagePrice", 0))
                current_price = float(pos.get("currentPrice", 0))
                ppl_eur       = float(pos.get("ppl", 0)) + float(pos.get("fxPpl", 0))
                approx_inv    = (qty * avg_price) / 1.112
                pnl_pct       = ppl_eur / approx_inv if approx_inv > 0 else 0
                log.info(
                    f"   💤 {t212_ticker:12s} "
                    f"Rendimiento: {pnl_pct*100:+.2f}%  [en espera]"
                )
            log.info(f"   ⏰ Mercado abre L-V a las 15:30. Próxima comprobación en 60s.")
            time.sleep(60)
            continue

        # ── 1. Gestionar posiciones abiertas ──────────────────────────────────
        for pos in portfolio:
            t212_ticker   = pos.get("ticker", "")
            if t212_ticker in sell_pending_tkrs:
                log.info(f"   ⏳ {t212_ticker:12s} Venta en proceso...")
                continue

            qty           = float(pos.get("quantity", 0))
            avg_price     = float(pos.get("averagePrice", 0))
            current_price = float(pos.get("currentPrice", 0))
            ppl_eur       = float(pos.get("ppl", 0)) + float(pos.get("fxPpl", 0))

            approx_invested_eur = (qty * avg_price) / 1.112
            pnl_pct = (ppl_eur / approx_invested_eur) if approx_invested_eur > 0 else 0

            prev_max = high_water_marks.get(t212_ticker, avg_price)
            if current_price > prev_max:
                high_water_marks[t212_ticker] = current_price
                prev_max = current_price

            is_trailing = trailing_active.get(t212_ticker, False)
            trail_tag   = "🎯ARMED" if is_trailing else "     "
            log.info(
                f"   💼 {t212_ticker:12s} {trail_tag} "
                f"PnL={pnl_pct*100:+.2f}%"
            )

            # Hard Stop Loss
            if pnl_pct <= -HARD_SL:
                log.warning(f"🚨 {t212_ticker} HARD STOP LOSS ({pnl_pct*100:.2f}%). Cortando...")
                if sell_position_market(t212_ticker, qty, "HARD_SL", dry_run):
                    high_water_marks.pop(t212_ticker, None)
                    trailing_active.pop(t212_ticker, None)
                continue

            # Trailing Stop
            if pnl_pct >= TRAIL_ACTIVATE:
                if not trailing_active.get(t212_ticker, False):
                    log.info(f"   ✅ {t212_ticker} TRAILING ARMADO a +{pnl_pct*100:.2f}%")
                trailing_active[t212_ticker] = True

            if trailing_active.get(t212_ticker, False):
                drawdown = (prev_max - current_price) / prev_max if prev_max > 0 else 0
                if drawdown >= TRAIL_CALLBACK or pnl_pct <= 0.001:
                    reason = f"drawdown {drawdown*100:.2f}%" if drawdown >= TRAIL_CALLBACK else "breakeven"
                    log.info(f"🎯 {t212_ticker} TRAILING CERRADO ({reason}). Vendiendo...")
                    if sell_position_market(t212_ticker, qty, "TRAILING_TP", dry_run):
                        high_water_marks.pop(t212_ticker, None)
                        trailing_active.pop(t212_ticker, None)
                    continue

        # ── 2. Escaneo de Momentum Alcista en todo el Universo (15:45 – 21:00) ─
        if not ventana_compras:
            log.info("   🛡️ Fuera de ventana de compras (solo se permite abrir entre 15:45 y 21:00). Gestionando cartera...")
        elif active_count < MAX_POSITIONS and free_eur >= 2.00:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            candidates = []

            def eval_ticker(item):
                y_sym, t212_sym = item
                if t212_sym in held_tickers:
                    return None
                try:
                    price, rsi, closes, vol_actual, vol_media, ema20, ema50 = get_market_data(y_sym)
                    if price <= 0 or len(closes) < 10:
                        return None
                    trend_ok    = price > ema20 and ema20 > ema50
                    rsi_ok      = 50.0 <= rsi <= 70.0
                    momentum_ok = closes[-1] >= closes[-3] if len(closes) >= 3 else True
                    vol_ok      = vol_actual >= (vol_media * 0.70) if vol_media > 0 else True
                    if trend_ok and rsi_ok and momentum_ok and vol_ok:
                        trend_strength = (price - ema50) / ema50
                        vol_ratio = vol_actual / vol_media if vol_media > 0 else 1.0
                        score = (trend_strength * 100) + (rsi - 50) + vol_ratio
                        return {
                            "y_sym": y_sym,
                            "t212_sym": t212_sym,
                            "price": price,
                            "rsi": rsi,
                            "score": score
                        }
                except Exception:
                    return None
                return None

            with ThreadPoolExecutor(max_workers=16) as pool:
                futures = [pool.submit(eval_ticker, item) for item in UNIVERSE.items()]
                for fut in as_completed(futures):
                    res = fut.result()
                    if res:
                        candidates.append(res)

            if candidates:
                # Ordenar por el mayor score de momentum
                candidates.sort(key=lambda x: x["score"], reverse=True)
                top = candidates[0]

                eur_per_trade = total_eur / MAX_POSITIONS
                eur_to_spend  = min(free_eur * 0.95, eur_per_trade)
                approx_usd    = eur_to_spend * 1.08
                shares_qty    = approx_usd / top["price"]

                log.info(f"🔥 SEÑAL MOMENTUM: {top['y_sym']} (RSI={top['rsi']:.1f})")
                log.info(f"   🚀 Abriendo posición en {top['y_sym']}...")
                if place_order_market(top["t212_sym"], shares_qty, dry_run):
                    high_water_marks[top["t212_sym"]] = top["price"]
                    held_tickers.add(top["t212_sym"])
            else:
                log.info(f"   ⏳ Escaneando los {len(UNIVERSE)} activos: esperando que surja un setup claro de impulso...")

        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bot Trading 212 Momentum v4.2")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-minutes", type=int, default=0, help="Minutos máximos de ejecución (0=infinito)")
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run, max_minutes=args.max_minutes)
    except KeyboardInterrupt:
        log.info("\n🛑 Bot detenido.")
