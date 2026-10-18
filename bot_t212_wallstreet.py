#!/usr/bin/env python3
"""
bot_t212_wallstreet.py — Bot Cuantitativo Multi-Estrategia para Trading 212
================================================================================
Estrategia v5.0 (Dual Engine: Trend Momentum + Mean Reversion + Market Regime):
  1. Filtro de Régimen de Mercado (SPY S&P 500):
     - Si el S&P 500 está en caída libre (SPY < EMA20 y RSI < 45), frena las compras
       agresivas por rotura (evita comprar techos en días bajistas).
     - Solo permite compras por Reversión a la Media (sobreventa extrema en soporte).
  2. Motor Dual de Selección de Activos:
     - Estrategia A [MOMENTUM]:
         * Tendencia institucional: Precio > EMA(20) > EMA(50).
         * Momentum equilibrado: 50 <= RSI <= 68.
         * Filtro Anti-Techo: Precio <= Banda Superior Bollinger (no entra sobreextendido).
         * Confirmación de giro: Vela actual verde (compradores al mando).
         * Volumen consistente >= 70% de la media de 20 velas.
     - Estrategia B [MEAN REVERSION - Cazador de Rebotes]:
         * Sobreventa extrema: RSI <= 32.
         * Precio perforando o en contacto con Banda Inferior Bollinger (descuento estadístico).
         * Confirmación de rebote: Vela actual verde (compradores frenando la caída).
  3. Gestión de Riesgo Triple Escudo:
     - Stop Loss Dinámico / ATR: Entre -0.9% y -1.4% según volatilidad real.
     - Breakeven Lock (+0.65%): Al tocar +0.65%, el Stop Loss sube a +0.08% garantizando
       que una operación ganadora jamás termine en pérdidas.
     - Trailing Stop Dinámico (+1.10%): Se arma en +1.10% con callback de 0.40% desde el pico.
     - Cierre Intradía 21:50 Madrid: Cierre forzoso de posiciones bot para dormir 100% en liquidez.
"""

import os
import sys
import time
import json
import math
import logging
import argparse
import pathlib
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
import requests

# ── Codificación UTF-8 ────────────────────────────────────────────────────────
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

# Activos intocables (regalos promocionales del broker como las acciones de NVDA)
EXCLUDED_PORTFOLIO_TICKERS = {"NVDA_US_EQ"}

# ── Parámetros Cuantitativos v5.0 ─────────────────────────────────────────────
RSI_PERIOD          = 14
RSI_MOM_MIN         = 50.0
RSI_MOM_MAX         = 68.0    # Evitar sobrecompra extrema
RSI_OVERSOLD_MAX    = 32.0    # Umbral de sobreventa para Mean Reversion

EMA_SHORT           = 20
EMA_LONG            = 50

# Gestión de Salidas
DEFAULT_HARD_SL     = 0.012   # -1.20% Stop Loss base
BREAKEVEN_TRIGGER   = 0.0065  # A partir de +0.65% se asegura la operación
BREAKEVEN_LOCK      = 0.0008  # Salida garantizada en +0.08% (cubre spread/comisión FX)
TRAIL_ACTIVATE      = 0.0110  # Trailing Stop se arma a +1.10%
TRAIL_CALLBACK      = 0.0040  # Callback de 0.40% desde el máximo alcanzado

LOOP_SECONDS        = 20
MAX_POSITIONS       = 4       # Slots para diversificar capital

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
    UNIVERSE = {"AAPL": "AAPL_US_EQ", "WMT": "WMT_US_EQ", "MSFT": "MSFT_US_EQ"}

# ── Funciones de API Trading 212 ──────────────────────────────────────────────

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


# ── Cálculo de Indicadores Técnicos ───────────────────────────────────────────

def calc_ema(prices: list, period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def get_market_data(symbol: str) -> dict:
    """
    Descarga velas de 5m (rango 5 días) de Yahoo Finance y computa
    EMA, RSI, Bandas de Bollinger, ATR y volumen.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=5d"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data["chart"]["result"][0]
        quotes = result["indicators"]["quote"][0]

        raw_opens   = quotes.get("open", [])
        raw_highs   = quotes.get("high", [])
        raw_lows    = quotes.get("low", [])
        raw_closes  = quotes.get("close", [])
        raw_volumes = quotes.get("volume", [])

        paired = [
            (o, h, l, c, v)
            for o, h, l, c, v in zip(raw_opens, raw_highs, raw_lows, raw_closes, raw_volumes)
            if o is not None and h is not None and l is not None and c is not None and v is not None
        ]

        if len(paired) < EMA_LONG + 5:
            return None

        opens   = [p[0] for p in paired]
        highs   = [p[1] for p in paired]
        lows    = [p[2] for p in paired]
        closes  = [p[3] for p in paired]
        volumes = [p[4] for p in paired]

        price = closes[-1]
        if price <= 0:
            return None

        ema20 = calc_ema(closes, EMA_SHORT)
        ema50 = calc_ema(closes, EMA_LONG)

        # RSI(14)
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas]
        losses = [max(-d, 0) for d in deltas]
        avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
        avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
        for g, l in zip(gains[RSI_PERIOD:], losses[RSI_PERIOD:]):
            avg_gain = (avg_gain * (RSI_PERIOD - 1) + g) / RSI_PERIOD
            avg_loss = (avg_loss * (RSI_PERIOD - 1) + l) / RSI_PERIOD
        rs  = avg_gain / avg_loss if avg_loss > 0 else 100.0
        rsi = round(100 - 100 / (1 + rs), 2)

        # Bandas de Bollinger (20 periodos, 2 desviaciones)
        sma20 = sum(closes[-20:]) / 20
        variance = sum((x - sma20) ** 2 for x in closes[-20:]) / 20
        std = math.sqrt(variance)
        bb_upper = sma20 + (2.0 * std)
        bb_lower = sma20 - (2.0 * std)

        # ATR(14)
        trs = []
        for i in range(1, len(closes)):
            h, l, cp = highs[i], lows[i], closes[i - 1]
            trs.append(max(h - l, abs(h - cp), abs(l - cp)))
        atr14   = sum(trs[-14:]) / 14 if len(trs) >= 14 else (highs[-1] - lows[-1])
        atr_pct = (atr14 / price) if price > 0 else 0.01

        vol_actual = volumes[-1] if volumes[-1] > 0 else (volumes[-2] if len(volumes) >= 2 else 0)
        vol_media  = sum(volumes[-21:-1]) / min(20, len(volumes) - 1) if len(volumes) > 1 else 1.0

        # Vela verde / compradores activos
        is_green = closes[-1] >= opens[-1] or (len(closes) >= 2 and closes[-1] >= closes[-2])

        return {
            "price": price,
            "rsi": rsi,
            "ema20": ema20,
            "ema50": ema50,
            "sma20": sma20,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "atr_pct": atr_pct,
            "vol_actual": vol_actual,
            "vol_media": vol_media,
            "is_green": is_green,
            "closes": closes,
        }
    except Exception:
        return None


def get_market_regime() -> str:
    """
    Evalúa el estado del índice director SPY (S&P 500 ETF).
    Retorna: 'BULLISH', 'BEARISH_PULLBACK', o 'NEUTRAL'.
    """
    spy_data = get_market_data("SPY")
    if not spy_data:
        return "NEUTRAL"
    price = spy_data["price"]
    ema20 = spy_data["ema20"]
    rsi   = spy_data["rsi"]

    if price < ema20 and rsi < 45.0:
        return "BEARISH_PULLBACK"
    elif price >= ema20 and rsi >= 48.0:
        return "BULLISH"
    return "NEUTRAL"


# ── Estados de Posición ───────────────────────────────────────────────────────
high_water_marks: dict = {}
trailing_active: dict  = {}
breakeven_active: dict = {}
position_sl_pct: dict  = {}


def run(dry_run: bool, max_minutes: int = 0):
    log.info("=" * 75)
    log.info("🚀 TRADING 212 — WALL STREET DUAL QUANTITATIVE ENGINE v5.0")
    log.info(f"   Universo ({len(UNIVERSE)} activos): Multi-sectorial (Metales, Energía, Consumo, Tech, Cripto)")
    log.info(f"   Estrategias   : A) Trend Momentum (Anti-Techo)  |  B) Mean Reversion (Rebotes)")
    log.info(f"   Riesgo        : Breakeven Lock (+{BREAKEVEN_TRIGGER*100:.2f}%) | Trailing (+{TRAIL_ACTIVATE*100:.2f}%, cb -{TRAIL_CALLBACK*100:.2f}%)")
    log.info(f"   Liquidación   : 21:50 Madrid (100% liquidez nocturna y fines de semana)")
    if max_minutes > 0:
        log.info(f"   Duración máx. : {max_minutes} minutos (Modo Cloud / GitHub Actions)")
    log.info(f"   Modo          : {'DRY-RUN' if dry_run else 'TRADING REAL'}")
    log.info("=" * 75)

    start_ts = time.time()
    loop = 0
    last_regime_check = 0
    current_regime = "NEUTRAL"

    while True:
        if max_minutes > 0 and (time.time() - start_ts) >= max_minutes * 60:
            log.info(f"⏹️ Turno de {max_minutes} minutos completado limpiamente. Cerrando proceso.")
            break
        loop += 1
        now_local = datetime.now()
        now_str   = now_local.strftime("%H:%M:%S")

        hora_min = now_local.hour * 60 + now_local.minute
        es_dia_laborable = now_local.weekday() < 5   # 0=Lunes ... 4=Viernes
        NYSE_OPEN        = 15 * 60 + 30   # 15:30 Madrid (Apertura mercado)
        NYSE_BUY_START   = 15 * 60 + 45   # 15:45 Madrid (Filtro primeros 15m)
        NYSE_BUY_STOP    = 21 * 60 + 0    # 21:00 Madrid (Fin apertura compras)
        NYSE_FORCE_CLOSE = 21 * 60 + 50   # 21:50 Madrid (Cierre forzoso de seguridad)
        NYSE_CLOSE       = 22 * 60 + 0    # 22:00 Madrid

        mercado_abierto  = es_dia_laborable and (NYSE_OPEN <= hora_min < NYSE_CLOSE)
        ventana_compras  = es_dia_laborable and (NYSE_BUY_START <= hora_min < NYSE_BUY_STOP)

        cash         = get_cash()
        free_eur     = float(cash.get("free", 0))
        total_eur    = float(cash.get("total", free_eur))
        raw_port     = get_portfolio()
        orders       = get_pending_orders()

        # Filtrar cartera: Excluir regalos de T212 (NVDA_US_EQ) para no tocarlos jamás
        portfolio = [
            p for p in raw_port
            if p.get("ticker") not in EXCLUDED_PORTFOLIO_TICKERS
            and p.get("frontend") != "SYSTEM"
        ]

        buy_pending_tkrs  = {o.get("ticker") for o in orders
                             if o.get("status") in ("NEW", "SUBMITTED", "PENDING")
                             and float(o.get("quantity", 0)) > 0}
        sell_pending_tkrs = {o.get("ticker") for o in orders
                             if o.get("status") in ("NEW", "SUBMITTED", "PENDING")
                             and float(o.get("quantity", 0)) < 0}

        held_tickers = {p.get("ticker") for p in portfolio} | buy_pending_tkrs | EXCLUDED_PORTFOLIO_TICKERS
        active_count = len(portfolio) + len(buy_pending_tkrs)

        # ── REGLA INTRADÍA: Cierre forzoso a las 21:50 ─────────────────────────
        if es_dia_laborable and (NYSE_FORCE_CLOSE <= hora_min < NYSE_CLOSE) and portfolio:
            log.warning("🌙 CIERRE INTRADÍA PREVENTIVO (21:50). Liquidando posiciones bot para riesgo cero nocturno...")
            for pos in portfolio:
                t212_ticker = pos.get("ticker", "")
                if t212_ticker in sell_pending_tkrs:
                    continue
                qty = float(pos.get("quantity", 0))
                sell_position_market(t212_ticker, qty, "CIERRE_NOCTURNO", dry_run)
                high_water_marks.pop(t212_ticker, None)
                trailing_active.pop(t212_ticker, None)
                breakeven_active.pop(t212_ticker, None)
                position_sl_pct.pop(t212_ticker, None)
            time.sleep(LOOP_SECONDS)
            continue

        # Actualizar régimen de mercado cada 3 minutos
        if time.time() - last_regime_check > 180:
            current_regime = get_market_regime()
            last_regime_check = time.time()

        regime_icon = "🟢 ALCISTA" if current_regime == "BULLISH" else ("🔴 CAÍDA (SPY débil)" if current_regime == "BEARISH_PULLBACK" else "🟡 NEUTRAL")

        log.info(
            f"\n--- [Ciclo #{loop} {now_str}] "
            f"Slots bot: {len(portfolio)}/{MAX_POSITIONS} | "
            f"SPY: {regime_icon} | "
            f"{'🟢 MERCADO ABIERTO' if mercado_abierto else '🔴 MERCADO CERRADO'} ---"
        )

        if not mercado_abierto:
            for pos in portfolio:
                t212_ticker   = pos.get("ticker", "")
                qty           = float(pos.get("quantity", 0))
                avg_price     = float(pos.get("averagePrice", 0))
                ppl_eur       = float(pos.get("ppl", 0)) + float(pos.get("fxPpl", 0))
                approx_inv    = (qty * avg_price) / 1.112
                pnl_pct       = ppl_eur / approx_inv if approx_inv > 0 else 0
                log.info(f"   💤 {t212_ticker:12s} Rendimiento: {pnl_pct*100:+.2f}%  [en espera de apertura]")
            time.sleep(60)
            continue

        # ── 1. GESTIÓN ACTIVA DE POSICIONES ABIERTAS (TRIPLE ESCUDO) ───────────
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
            is_be_lock  = breakeven_active.get(t212_ticker, False)

            status_tag = "🎯TRAILING" if is_trailing else ("🛡️BREAKEVEN" if is_be_lock else "          ")
            log.info(f"   💼 {t212_ticker:12s} {status_tag} PnL={pnl_pct*100:+.2f}%")

            # A) ESCUDO 1: HARD STOP LOSS DINÁMICO
            max_sl = position_sl_pct.get(t212_ticker, DEFAULT_HARD_SL)
            if pnl_pct <= -max_sl:
                log.warning(f"🚨 {t212_ticker} STOP LOSS ({pnl_pct*100:.2f}% <= -{max_sl*100:.2f}%). Protegiendo capital...")
                if sell_position_market(t212_ticker, qty, "HARD_SL", dry_run):
                    high_water_marks.pop(t212_ticker, None)
                    trailing_active.pop(t212_ticker, None)
                    breakeven_active.pop(t212_ticker, None)
                    position_sl_pct.pop(t212_ticker, None)
                continue

            # B) ESCUDO 2: BREAKEVEN LOCK (+0.65%)
            if pnl_pct >= BREAKEVEN_TRIGGER and not is_be_lock:
                breakeven_active[t212_ticker] = True
                log.info(f"   🛡️ {t212_ticker} BREAKEVEN BLOQUEADO a +{pnl_pct*100:.2f}%. Pérdidas eliminadas.")

            if breakeven_active.get(t212_ticker, False) and not is_trailing:
                if pnl_pct <= BREAKEVEN_LOCK:
                    log.info(f"🛡️ {t212_ticker} SALIDA BREAKEVEN (+{pnl_pct*100:.2f}%). Ganancia asegurada.")
                    if sell_position_market(t212_ticker, qty, "BREAKEVEN_LOCK", dry_run):
                        high_water_marks.pop(t212_ticker, None)
                        trailing_active.pop(t212_ticker, None)
                        breakeven_active.pop(t212_ticker, None)
                        position_sl_pct.pop(t212_ticker, None)
                    continue

            # C) ESCUDO 3: TRAILING STOP DINÁMICO (+1.10%)
            if pnl_pct >= TRAIL_ACTIVATE:
                if not is_trailing:
                    log.info(f"   🎯 {t212_ticker} TRAILING STOP ARMADO a +{pnl_pct*100:.2f}%")
                trailing_active[t212_ticker] = True

            if trailing_active.get(t212_ticker, False):
                drawdown = (prev_max - current_price) / prev_max if prev_max > 0 else 0
                if drawdown >= TRAIL_CALLBACK or pnl_pct <= BREAKEVEN_LOCK:
                    reason = f"callback {drawdown*100:.2f}% desde max" if drawdown >= TRAIL_CALLBACK else "protección breakeven"
                    log.info(f"🎯 {t212_ticker} TRAILING TAKE PROFIT ({reason}). Realizando beneficios...")
                    if sell_position_market(t212_ticker, qty, "TRAILING_TP", dry_run):
                        high_water_marks.pop(t212_ticker, None)
                        trailing_active.pop(t212_ticker, None)
                        breakeven_active.pop(t212_ticker, None)
                        position_sl_pct.pop(t212_ticker, None)
                    continue

        # ── 2. ESCANEO PARALELO DUAL ENGINE (MOMENTUM & REVERSION) ────────────
        if not ventana_compras:
            log.info("   🛡️ Fuera de ventana de compras (solo 15:45 – 21:00). Monitorizando salidas...")
        elif active_count < MAX_POSITIONS and free_eur >= 2.00:
            candidates = []

            def eval_ticker(item):
                y_sym, t212_sym = item
                if t212_sym in held_tickers:
                    return None
                try:
                    d = get_market_data(y_sym)
                    if not d:
                        return None

                    price       = d["price"]
                    rsi         = d["rsi"]
                    ema20       = d["ema20"]
                    ema50       = d["ema50"]
                    bb_upper    = d["bb_upper"]
                    bb_lower    = d["bb_lower"]
                    atr_pct     = d["atr_pct"]
                    vol_actual  = d["vol_actual"]
                    vol_media   = d["vol_media"]
                    is_green    = d["is_green"]
                    closes      = d["closes"]

                    # Stop loss adaptativo por volatilidad (entre 0.9% y 1.4%)
                    adaptive_sl = max(0.009, min(0.014, 1.6 * atr_pct))

                    # ─────────────────────────────────────────────────────────
                    # ESTRATEGIA A: TREND MOMENTUM (Con filtro Anti-Techo)
                    # ─────────────────────────────────────────────────────────
                    if current_regime != "BEARISH_PULLBACK":
                        trend_ok      = (price > ema20) and (ema20 > ema50)
                        rsi_ok        = (RSI_MOM_MIN <= rsi <= RSI_MOM_MAX)
                        anti_top_ok   = (price <= bb_upper * 1.002)   # No comprar sobreextendido
                        vol_ok        = (vol_actual >= vol_media * 0.70) if vol_media > 0 else True
                        momentum_dir  = (closes[-1] >= closes[-3]) if len(closes) >= 3 else True

                        if trend_ok and rsi_ok and anti_top_ok and is_green and vol_ok and momentum_dir:
                            trend_dist = (price - ema50) / ema50
                            vol_ratio  = (vol_actual / vol_media) if vol_media > 0 else 1.0
                            score = (trend_dist * 80) + (rsi - 50) + (vol_ratio * 1.5)
                            return {
                                "y_sym": y_sym,
                                "t212_sym": t212_sym,
                                "price": price,
                                "rsi": rsi,
                                "strategy": "MOMENTUM",
                                "adaptive_sl": adaptive_sl,
                                "score": score,
                            }

                    # ─────────────────────────────────────────────────────────
                    # ESTRATEGIA B: MEAN REVERSION (Cazador de Rebotes en Soporte)
                    # ─────────────────────────────────────────────────────────
                    oversold_ok = (rsi <= RSI_OVERSOLD_MAX)
                    at_support  = (price <= bb_lower * 1.003)         # En/debajo de banda inferior
                    bounce_ok   = is_green                            # Vela verde de confirmación
                    vol_bounce  = (vol_actual >= vol_media * 0.50) if vol_media > 0 else True

                    if oversold_ok and at_support and bounce_ok and vol_bounce:
                        score = ((35.0 - rsi) * 2.5) + (((bb_lower - price) / bb_lower) * 100)
                        return {
                            "y_sym": y_sym,
                            "t212_sym": t212_sym,
                            "price": price,
                            "rsi": rsi,
                            "strategy": "REVERSION_REBOTE",
                            "adaptive_sl": adaptive_sl,
                            "score": score,
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
                candidates.sort(key=lambda x: x["score"], reverse=True)
                top = candidates[0]

                eur_per_trade = total_eur / MAX_POSITIONS
                eur_to_spend  = min(free_eur * 0.95, eur_per_trade)
                approx_usd    = eur_to_spend * 1.08
                shares_qty    = approx_usd / top["price"]

                strat_label = "🔥 IMPULSO ALCISTA" if top["strategy"] == "MOMENTUM" else "💎 REBOTE SOBREVENTA"
                log.info(f"{strat_label}: {top['y_sym']} (RSI={top['rsi']:.1f}, SL={top['adaptive_sl']*100:.2f}%)")
                log.info(f"   🚀 Abriendo posición en {top['y_sym']} [{top['strategy']}]...")

                if place_order_market(top["t212_sym"], shares_qty, dry_run):
                    high_water_marks[top["t212_sym"]] = top["price"]
                    position_sl_pct[top["t212_sym"]]  = top["adaptive_sl"]
                    held_tickers.add(top["t212_sym"])
            else:
                log.info(f"   ⏳ Escaneando {len(UNIVERSE)} activos: esperando confirmación de setup de alta probabilidad...")

        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bot Trading 212 Dual Engine v5.0")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-minutes", type=int, default=0, help="Minutos de ejecución (0=infinito)")
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run, max_minutes=args.max_minutes)
    except KeyboardInterrupt:
        log.info("\n🛑 Bot detenido.")
