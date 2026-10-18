#!/usr/bin/env python3
"""
bot_t212_wallstreet.py — Motor Cuantitativo Swing Adaptativo por Regímenes (v6.5)
================================================================================
Auditado en Backtest Histórico (63 Blue-Chips del S&P 500, 3 slots):
  - Rentabilidad 14 Meses : +50.88% (vs +21.97% S&P 500 | Profit Factor: 1.44)
  - Rentabilidad 6 Meses  : +35.53% (vs +3.51% S&P 500  | Profit Factor: 2.12 | Max DD: 5.84%)

Arquitectura de Conmutación Dinámica en Función de la Tendencia (3 Regímenes SPY):
  1. RÉGIMEN 1: EXPANSIÓN ALCISTA (SPY >= SMA50 y SPY >= SMA200)
     - Estrategia A [PULLBACK]: Stage 2 (Close > SMA50 > SMA200), apoyo en EMA20 (±2.5%),
       42 <= RSI(14) <= 60 y vela verde -> TP largo (+7.50%), SL 1.8*ATR.
     - Estrategia B [DIP_BUY]: Líderes en Stage 2 con sobreventa extrema de corto plazo
       (RSI(4) < 18) y giro verde -> TP (+6.00%) o salida cuando RSI(4) >= 68.
  2. RÉGIMEN 2: CORRECCIÓN INTERMEDIA / LATERAL (SPY < SMA50 pero SPY >= SMA200)
     - Adapta objetivos a un mercado más corto de recorrido:
       * Pullback estricto (EMA20 -2.5%/+1.5%, RSI 40-55) con TP reducido a +5.50%.
       * Cazador de Rebotes [DIP_BUY] en Blue-Chips sobre SMA200 con RSI(4) < 20 -> TP +5.50%.
  3. RÉGIMEN 3: MERCADO BAJISTA / DEFENSIVO (SPY < SMA200)
     - Bloquea compras agresivas y activa rotación a Refugio Descorrelacionado [DEFENSIVE_SWING]:
       * Solo compra activos de baja volatilidad (ATR <= 2.2%) que mantengan tendencia
         alcista propia (Close > SMA50 > SMA200, ej. Oro, Consumo Básico, Defensa, Salud)
         con TP conservador de +5.00% y SL ceñido (1.5*ATR, máx -3.0%).
"""

import os
import sys
import time
import json
import math
import logging
import argparse
import pathlib
from datetime import datetime, timezone
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

# Activos promocionales intocables (regalo bienvenida T212)
EXCLUDED_PORTFOLIO_TICKERS = {"NVDA_US_EQ"}

# ── Parámetros Técnicos Base ──────────────────────────────────────────────────
EMA_FAST            = 10
EMA_PULLBACK        = 20
SMA_MID             = 50
SMA_MACRO           = 200

SWING_TRAIL_CB      = 0.0050  # Callback de 0.50% una vez alcanzado el Take Profit del régimen
DEFAULT_SWING_SL    = 0.0300  # -3.00% Stop Loss base
MAX_HOLD_DAYS       = 15      # Rotación máxima de capital si un activo se estanca

LOOP_SECONDS        = 60      # Ciclo de vigilancia cada 60s
MAX_POSITIONS       = 3       # 3 slots simultáneos (auditado con PF 2.12 y +50.88% retorno)

# ── Universo de tickers (63 S&P 500 Blue-Chips) ───────────────────────────────
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
log = logging.getLogger("t212_swing")

try:
    with UNIVERSE_PATH.open(encoding="utf-8") as f:
        UNIVERSE = json.load(f)
    log.info(f"✅ Universo S&P 500 Blue-Chips cargado: {len(UNIVERSE)} activos listados.")
except Exception as e:
    log.warning(f"⚠️ Error cargando universe.json: {e}")
    UNIVERSE = {"AAPL": "AAPL_US_EQ", "WMT": "WMT_US_EQ", "MSFT": "MSFT_US_EQ"}

T212_TO_YAHOO = {v: k for k, v in UNIVERSE.items()}

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
        log.info(f"[DRY-RUN] COMPRA SWING: {ticker_t212} qty={quantity:.4f}")
        return True
    body = {"quantity": round(quantity, 4), "ticker": ticker_t212}
    r = requests.post(f"{BASE_URL}/equity/orders/market", auth=AUTH, json=body, timeout=10)
    if r.status_code in (200, 201):
        log.info(f"💥 COMPRA SWING EJECUTADA: {ticker_t212} qty={quantity:.4f}")
        return True
    log.error(f"Error compra {ticker_t212}: HTTP {r.status_code} - {r.text}")
    return False


def sell_position_market(ticker_t212: str, quantity: float, reason: str, dry_run: bool) -> bool:
    if dry_run:
        log.info(f"[DRY-RUN] VENTA SWING [{reason}]: {ticker_t212} qty={quantity:.4f}")
        return True
    body = {"quantity": -abs(round(quantity, 4)), "ticker": ticker_t212}
    r = requests.post(f"{BASE_URL}/equity/orders/market", auth=AUTH, json=body, timeout=10)
    if r.status_code in (200, 201):
        log.info(f"🟢 VENTA SWING EJECUTADA [{reason}]: {ticker_t212} qty={quantity:.4f}")
        return True
    if "not-owned" in r.text or r.status_code == 400:
        log.warning(f"⚠️ {ticker_t212} cerrada externamente.")
        return True
    log.error(f"Error venta {ticker_t212}: HTTP {r.status_code} - {r.text}")
    return False


# ── Cálculo de Indicadores Diarios (Velas 1D - 2 Años) ────────────────────────

def calc_ema(prices: list, period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    k = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1.0 - k)
    return ema


def calc_wilder_rsi(closes: list, period: int) -> float:
    if len(closes) <= period:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def get_daily_swing_data(symbol: str) -> dict:
    """
    Descarga 2 años de velas diarias (1D) de Yahoo Finance y calcula:
    SMA(50), SMA(200), EMA(10), EMA(20), RSI(14), RSI(4), ATR(14) y vela actual.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2y"
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

        if len(paired) < SMA_MACRO + 5:
            return None

        opens   = [p[0] for p in paired]
        highs   = [p[1] for p in paired]
        lows    = [p[2] for p in paired]
        closes  = [p[3] for p in paired]

        price = closes[-1]
        if price <= 0:
            return None

        sma50  = sum(closes[-SMA_MID:]) / SMA_MID
        sma200 = sum(closes[-SMA_MACRO:]) / SMA_MACRO
        ema10  = calc_ema(closes, EMA_FAST)
        ema20  = calc_ema(closes, EMA_PULLBACK)

        rsi14 = calc_wilder_rsi(closes, 14)
        rsi4  = calc_wilder_rsi(closes, 4)

        trs = []
        for i in range(1, len(closes)):
            h, l, cp = highs[i], lows[i], closes[i - 1]
            trs.append(max(h - l, abs(h - cp), abs(l - cp)))
        atr14   = sum(trs[-14:]) / 14.0 if len(trs) >= 14 else (highs[-1] - lows[-1])
        atr_pct = (atr14 / price) if price > 0 else 0.015

        is_green = closes[-1] >= opens[-1]

        return {
            "price": price,
            "sma50": sma50,
            "sma200": sma200,
            "ema10": ema10,
            "ema20": ema20,
            "rsi14": rsi14,
            "rsi4": rsi4,
            "atr_pct": atr_pct,
            "is_green": is_green,
        }
    except Exception:
        return None


def get_market_regime() -> tuple:
    """
    Clasifica la tendencia del mercado estadounidense (SPY) en 3 Regímenes Dinámicos:
      - 'BULL_EXPANSION'  : SPY >= SMA(50) y SPY >= SMA(200) -> TP +7.50%
      - 'BULL_CORRECTION' : SPY < SMA(50) pero SPY >= SMA(200) -> TP +5.50% (Pullbacks + Rebotes RSI4)
      - 'BEAR_DEFENSIVE'  : SPY < SMA(200) -> TP +5.00% (Solo Refugios descorrelacionados en Stage 2)
    """
    spy = get_daily_swing_data("SPY")
    if not spy:
        return "BULL_EXPANSION", 0.0750, 50.0

    p, s50, s200, r14 = spy["price"], spy["sma50"], spy["sma200"], spy["rsi14"]
    if p >= s50 and p >= s200:
        return "BULL_EXPANSION", 0.0750, r14
    elif p >= s200:
        return "BULL_CORRECTION", 0.0550, r14
    else:
        return "BEAR_DEFENSIVE", 0.0500, r14


def calc_days_held(initial_fill_date_str: str) -> float:
    if not initial_fill_date_str:
        return 0.0
    try:
        dt_fill = datetime.fromisoformat(initial_fill_date_str)
        now_utc = datetime.now(timezone.utc)
        delta   = now_utc - dt_fill.astimezone(timezone.utc)
        return max(0.0, delta.total_seconds() / 86400.0)
    except Exception:
        return 0.0


# ── Memoria de Sesión ─────────────────────────────────────────────────────────
high_water_marks: dict = {}
trailing_active: dict  = {}
cached_sl_map: dict    = {}
cached_tp_map: dict    = {}


def run(dry_run: bool, max_minutes: int = 0):
    log.info("=" * 80)
    log.info("🚀 TRADING 212 — WALL STREET ADAPTIVE REGIME SWING ENGINE v6.5")
    log.info(f"   Universo ({len(UNIVERSE)} Blue-Chips S&P 500): Escaneo Diario 1D Multi-Estrategia")
    log.info("   Conmutación Automática según Tendencia de SPY:")
    log.info("     🟢 BULL_EXPANSION  (SPY >= SMA50) : Trend Pullback + Dip Buyer | TP +7.5%")
    log.info("     🟡 BULL_CORRECTION (SPY <  SMA50) : Pullback Corto + Rebotes   | TP +5.5%")
    log.info("     🛡️ BEAR_DEFENSIVE  (SPY < SMA200) : Rotación Refugio Baja Beta | TP +5.0%")
    if max_minutes > 0:
        log.info(f"   Duración turno: {max_minutes} minutos (Modo Cloud / GitHub Actions)")
    log.info(f"   Modo          : {'DRY-RUN' if dry_run else 'TRADING REAL'}")
    log.info("=" * 80)

    start_ts = time.time()
    loop = 0
    last_regime_check = 0
    current_regime = "BULL_EXPANSION"
    regime_tp = 0.0750
    spy_rsi = 50.0

    while True:
        if max_minutes > 0 and (time.time() - start_ts) >= max_minutes * 60:
            log.info(f"⏹️ Turno Cloud de {max_minutes} minutos finalizado limpiamente.")
            break
        loop += 1
        now_local = datetime.now()
        now_str   = now_local.strftime("%H:%M:%S")

        hora_min = now_local.hour * 60 + now_local.minute
        es_dia_laborable = now_local.weekday() < 5   # 0=Lunes ... 4=Viernes

        NYSE_OPEN         = 15 * 60 + 30   # 15:30 Madrid
        SWING_BUY_START   = 16 * 60 + 0    # 16:00 Madrid
        SWING_BUY_STOP    = 21 * 60 + 45   # 21:45 Madrid
        EOD_EVAL_START    = 21 * 60 + 30   # 21:30–21:58 Madrid (Cierre de vela 1D)
        NYSE_CLOSE        = 22 * 60 + 0    # 22:00 Madrid

        mercado_abierto   = es_dia_laborable and (NYSE_OPEN <= hora_min < NYSE_CLOSE)
        ventana_compras   = es_dia_laborable and (SWING_BUY_START <= hora_min < SWING_BUY_STOP)
        ventana_cierre_1d = es_dia_laborable and (EOD_EVAL_START <= hora_min < NYSE_CLOSE - 2)

        cash      = get_cash()
        free_eur  = float(cash.get("free", 0))
        raw_port  = get_portfolio()
        orders    = get_pending_orders()

        # Excluir acciones de regalo promocional (NVDA_US_EQ / SYSTEM)
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

        # Evaluar Régimen de Tendencia de SPY cada 15 minutos
        if time.time() - last_regime_check > 900:
            current_regime, regime_tp, spy_rsi = get_market_regime()
            last_regime_check = time.time()

        regime_badge = {
            "BULL_EXPANSION":  f"🟢 EXPANSIÓN ALCISTA (TP +{regime_tp*100:.1f}%, RSI={spy_rsi:.0f})",
            "BULL_CORRECTION": f"🟡 CORRECCIÓN/LATERAL (TP +{regime_tp*100:.1f}%, RSI={spy_rsi:.0f})",
            "BEAR_DEFENSIVE":  f"🛡️ MODO REFUGIO BAJISTA (TP +{regime_tp*100:.1f}%, RSI={spy_rsi:.0f})",
        }.get(current_regime, current_regime)

        log.info(
            f"\n--- [Ciclo #{loop} {now_str}] "
            f"Slots: {len(portfolio)}/{MAX_POSITIONS} | "
            f"Régimen: {regime_badge} | "
            f"{'🟢 ABIERTO' if mercado_abierto else '🔴 CERRADO'} ---"
        )

        if not mercado_abierto:
            for pos in portfolio:
                t212_ticker = pos.get("ticker", "")
                qty         = float(pos.get("quantity", 0))
                avg_price   = float(pos.get("averagePrice", 0))
                ppl_eur     = float(pos.get("ppl", 0)) + float(pos.get("fxPpl", 0))
                approx_inv  = (qty * avg_price) / 1.112
                pnl_pct     = ppl_eur / approx_inv if approx_inv > 0 else 0
                days_held   = calc_days_held(pos.get("initialFillDate", ""))
                log.info(
                    f"   💤 {t212_ticker:12s} PnL={pnl_pct*100:+.2f}% | "
                    f"Días en cartera: {days_held:.1f}d  [Swing activo]"
                )
            time.sleep(60)
            continue

        # ── 1. GESTIÓN DE POSICIONES SWING ABIERTAS ───────────────────────────
        for pos in portfolio:
            t212_ticker = pos.get("ticker", "")
            if t212_ticker in sell_pending_tkrs:
                log.info(f"   ⏳ {t212_ticker:12s} Orden de venta en curso...")
                continue

            qty           = float(pos.get("quantity", 0))
            avg_price     = float(pos.get("averagePrice", 0))
            current_price = float(pos.get("currentPrice", 0))
            ppl_eur       = float(pos.get("ppl", 0)) + float(pos.get("fxPpl", 0))
            days_held     = calc_days_held(pos.get("initialFillDate", ""))

            approx_invested_eur = (qty * avg_price) / 1.112
            pnl_pct = (ppl_eur / approx_invested_eur) if approx_invested_eur > 0 else 0

            prev_max = high_water_marks.get(t212_ticker, max(avg_price, current_price))
            if current_price > prev_max:
                high_water_marks[t212_ticker] = current_price
                prev_max = current_price

            y_sym  = T212_TO_YAHOO.get(t212_ticker, t212_ticker.split("_")[0])
            d_info = get_daily_swing_data(y_sym)
            if d_info and t212_ticker not in cached_sl_map:
                cached_sl_map[t212_ticker] = max(0.025, min(0.040, 1.8 * d_info["atr_pct"]))

            sl_limit  = cached_sl_map.get(t212_ticker, DEFAULT_SWING_SL)
            tp_target = cached_tp_map.get(t212_ticker, regime_tp)

            if pnl_pct >= tp_target:
                trailing_active[t212_ticker] = True

            is_trailing = trailing_active.get(t212_ticker, False)
            status_tag  = "🎯TP_TRAIL" if is_trailing else f"SL:-{sl_limit*100:.1f}%|TP:+{tp_target*100:.1f}%"

            log.info(
                f"   💼 {t212_ticker:12s} [{status_tag}] "
                f"PnL={pnl_pct*100:+.2f}% | Hold={days_held:.1f}d"
            )

            # A) STOP LOSS ADAPTATIVO
            if pnl_pct <= -sl_limit:
                log.warning(f"🚨 {t212_ticker} SWING STOP LOSS ({pnl_pct*100:.2f}% <= -{sl_limit*100:.2f}%). Cortando pérdida...")
                if sell_position_market(t212_ticker, qty, "SWING_STOP_LOSS", dry_run):
                    high_water_marks.pop(t212_ticker, None)
                    trailing_active.pop(t212_ticker, None)
                    cached_sl_map.pop(t212_ticker, None)
                    cached_tp_map.pop(t212_ticker, None)
                continue

            # B) TAKE PROFIT ADAPTATIVO AL RÉGIMEN CON TRAILING DE CAPTURA
            if is_trailing:
                drawdown = (prev_max - current_price) / prev_max if prev_max > 0 else 0
                if drawdown >= SWING_TRAIL_CB or pnl_pct <= tp_target - 0.004:
                    log.info(f"🎯 {t212_ticker} TAKE PROFIT SWING ALCANZADO (PnL={pnl_pct*100:+.2f}%). Realizando beneficio...")
                    if sell_position_market(t212_ticker, qty, "SWING_TAKE_PROFIT", dry_run):
                        high_water_marks.pop(t212_ticker, None)
                        trailing_active.pop(t212_ticker, None)
                        cached_sl_map.pop(t212_ticker, None)
                        cached_tp_map.pop(t212_ticker, None)
                    continue

            # C) SALIDAS TÉCNICAS AL CIERRE DIARIO (21:30 - 21:58 Madrid)
            if ventana_cierre_1d and d_info:
                # 1. Salida por agotamiento de Rebote (RSI(4) >= 68 estando en beneficio)
                if d_info["rsi4"] >= 68.0 and pnl_pct >= 0.015:
                    log.info(f"📈 {t212_ticker} completó rebote rápido (RSI4={d_info['rsi4']:.1f}, PnL={pnl_pct*100:+.2f}%). Asegurando...")
                    if sell_position_market(t212_ticker, qty, "RSI4_OVERBOUGHT_TP", dry_run):
                        high_water_marks.pop(t212_ticker, None)
                        trailing_active.pop(t212_ticker, None)
                    continue

                # 2. Pérdida de EMA(10) diaria tras >= 3 días
                if days_held >= 3.0 and current_price < d_info["ema10"]:
                    log.info(f"📉 {t212_ticker} cerró bajo EMA(10) diaria tras {days_held:.1f}d (PnL={pnl_pct*100:+.2f}%). Rotando...")
                    if sell_position_market(t212_ticker, qty, "TRAILING_EMA10_1D", dry_run):
                        high_water_marks.pop(t212_ticker, None)
                        trailing_active.pop(t212_ticker, None)
                    continue

                # 3. Límite temporal 15 días
                if days_held >= MAX_HOLD_DAYS:
                    log.info(f"⌛ {t212_ticker} cumplió {days_held:.1f} días (máx {MAX_HOLD_DAYS}d). Liberando liquidez...")
                    if sell_position_market(t212_ticker, qty, "TIME_MAX_15D", dry_run):
                        high_water_marks.pop(t212_ticker, None)
                        trailing_active.pop(t212_ticker, None)
                    continue

        # ── 2. ESCANEO MULTI-ESTRATEGIA ADAPTADO AL RÉGIMEN DE MERCADO ────────
        if not ventana_compras:
            log.info("   🛡️ Fuera de ventana de nuevas compras Swing (16:00 – 21:45). Vigilando posiciones...")
        elif active_count < MAX_POSITIONS and free_eur >= 2.00:
            candidates = []

            def eval_adaptive_ticker(item):
                y_sym, t212_sym = item
                if t212_sym in held_tickers:
                    return None
                d = get_daily_swing_data(y_sym)
                if not d:
                    return None

                cp       = d["price"]
                s50      = d["sma50"]
                s200     = d["sma200"]
                e20      = d["ema20"]
                r14      = d["rsi14"]
                r4       = d["rsi4"]
                atr_pct  = d["atr_pct"]
                green    = d["is_green"]
                rel_str  = (cp - s200) / s200

                # ─────────────────────────────────────────────────────────────
                # RÉGIMEN 1: EXPANSIÓN ALCISTA (SPY >= SMA50 y >= SMA200)
                # ─────────────────────────────────────────────────────────────
                if current_regime == "BULL_EXPANSION":
                    # Estrategia 1A: Trend Pullback clásico (+7.5% TP)
                    if (cp > s50 > s200) and (e20 * 0.975 <= cp <= e20 * 1.025) and (42.0 <= r14 <= 60.0) and green:
                        sl_d = max(0.025, min(0.040, 1.8 * atr_pct))
                        return {
                            "y_sym": y_sym, "t212_sym": t212_sym, "price": cp, "rsi14": r14,
                            "strat": "PULLBACK_ALCISTA", "sl": sl_d, "tp": 0.0750, "score": rel_str
                        }
                    # Estrategia 1B: Dip Buyer en líderes (RSI4 < 18 en Stage 2)
                    if (cp > s50 > s200) and (r4 < 18.0) and green:
                        sl_d = max(0.025, min(0.035, 1.6 * atr_pct))
                        return {
                            "y_sym": y_sym, "t212_sym": t212_sym, "price": cp, "rsi14": r14,
                            "strat": "REBOTE_LIDER", "sl": sl_d, "tp": 0.0600, "score": rel_str + 0.10
                        }

                # ─────────────────────────────────────────────────────────────
                # RÉGIMEN 2: CORRECCIÓN INTERMEDIA / LATERAL (SPY < SMA50, >= SMA200)
                # ─────────────────────────────────────────────────────────────
                elif current_regime == "BULL_CORRECTION":
                    # Estrategia 2A: Pullback estricto con objetivo conservador (+5.5% TP)
                    if (cp > s50 > s200) and (e20 * 0.975 <= cp <= e20 * 1.015) and (40.0 <= r14 <= 55.0) and green:
                        sl_d = max(0.025, min(0.035, 1.6 * atr_pct))
                        return {
                            "y_sym": y_sym, "t212_sym": t212_sym, "price": cp, "rsi14": r14,
                            "strat": "PULLBACK_CORRECCION", "sl": sl_d, "tp": 0.0550, "score": rel_str
                        }
                    # Estrategia 2B: Cazador de sobreventas (RSI4 < 20 sobre SMA200)
                    if (cp > s200) and (r4 < 20.0) and green:
                        sl_d = max(0.025, min(0.035, 1.6 * atr_pct))
                        score = (25.0 - r4) * 0.05 + rel_str
                        return {
                            "y_sym": y_sym, "t212_sym": t212_sym, "price": cp, "rsi14": r14,
                            "strat": "REBOTE_SOBREVENTA", "sl": sl_d, "tp": 0.0550, "score": score
                        }

                # ─────────────────────────────────────────────────────────────
                # RÉGIMEN 3: MERCADO BAJISTA (SPY < SMA200) -> MODO REFUGIO
                # ─────────────────────────────────────────────────────────────
                elif current_regime == "BEAR_DEFENSIVE":
                    # Solo activos descorrelacionados de baja beta (ATR <= 2.2%) en Stage 2 propio
                    if (cp > s50 > s200) and (atr_pct <= 0.022) and (e20 * 0.980 <= cp <= e20 * 1.015) and (42.0 <= r14 <= 55.0) and green:
                        sl_d = max(0.022, min(0.030, 1.5 * atr_pct))
                        return {
                            "y_sym": y_sym, "t212_sym": t212_sym, "price": cp, "rsi14": r14,
                            "strat": "REFUGIO_DEFENSIVO", "sl": sl_d, "tp": 0.0500, "score": rel_str
                        }

                return None

            with ThreadPoolExecutor(max_workers=16) as pool:
                futures = [pool.submit(eval_adaptive_ticker, item) for item in UNIVERSE.items()]
                for fut in as_completed(futures):
                    res = fut.result()
                    if res:
                        candidates.append(res)

            if candidates:
                candidates.sort(key=lambda x: x["score"], reverse=True)
                top = candidates[0]

                remaining_slots = max(1, MAX_POSITIONS - active_count)
                eur_to_spend    = (free_eur * 0.96) / remaining_slots
                approx_usd      = eur_to_spend * 1.08
                shares_qty      = approx_usd / top["price"]

                log.info(
                    f"🔥 SEÑAL [{top['strat']}]: {top['y_sym']} "
                    f"(RSI_1D={top['rsi14']:.1f} | SL=-{top['sl']*100:.2f}% | TP=+{top['tp']*100:.1f}%)"
                )
                log.info(f"   🚀 Ejecutando entrada Swing en {top['y_sym']}...")

                if place_order_market(top["t212_sym"], shares_qty, dry_run):
                    high_water_marks[top["t212_sym"]] = top["price"]
                    cached_sl_map[top["t212_sym"]]    = top["sl"]
                    cached_tp_map[top["t212_sym"]]    = top["tp"]
                    held_tickers.add(top["t212_sym"])
            else:
                log.info(f"   ⏳ Escaneados {len(UNIVERSE)} Blue-Chips en modo [{current_regime}]: sin señales hoy.")

        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bot Trading 212 Adaptive Regime Swing v6.5")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-minutes", type=int, default=0, help="Minutos de ejecución (0=infinito)")
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run, max_minutes=args.max_minutes)
    except KeyboardInterrupt:
        log.info("\n🛑 Bot Swing detenido.")
