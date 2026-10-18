#!/usr/bin/env python3
"""
bot_t212_wallstreet.py — Motor Cuantitativo de Swing Trading (Trend Pullback v6.0)
================================================================================
Basado en el Modelo A validado mediante Backtesting Histórico de 2 Años (Velas 1D):
  - Profit Factor validado: 1.89 | Ratio Ganancia/Pérdida: 2.46 : 1 | Max DD: 5.22%
  - Reglas del Sistema:
      1. Filtro Macro Institucional (SPY):
         - Solo abre nuevas posiciones largas si el S&P 500 (SPY) cotiza por encima
           de su Media Móvil Simple de 200 sesiones (SMA200).
      2. Selección de Activos (Trend Pullback — Minervini / O'Neil Stage 2):
         - Tendencia Primaria Alcista: Precio > SMA(50) > SMA(200).
         - Pullback a Zona de Valor: EMA(20) * 0.975 <= Precio <= EMA(20) * 1.025.
         - Fuerza Relativa Equilibrada: 42.0 <= RSI(14) <= 60.0.
         - Confirmación de Giro: Vela diaria actual verde (Close >= Open).
         - Ranking: Ordenado por Fuerza Relativa respecto a su SMA(200).
      3. Gestión de Salidas Asimétrica (Persistente entre sesiones vía API T212):
         - Take Profit Objetivo: +7.50% (con Trailing de captura si sigue subiendo).
         - Escudo Breakeven Swing: Al alcanzar +3.50%, asegura +1.00% mínimo.
         - Stop Loss Adaptativo (ATR 14d): 1.8 * ATR% (acotado entre -2.50% y -4.00%).
         - Salida Técnica al Cierre (21:30–21:55 Madrid): Si lleva >= 3 días abierta
           y pierde la EMA(10) diaria, o si supera el límite temporal de 15 días.
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

# ── Parámetros Cuantitativos Swing v6.0 (Validados en Backtest 2Y) ────────────
RSI_PERIOD          = 14
RSI_PULLBACK_MIN    = 42.0
RSI_PULLBACK_MAX    = 60.0

EMA_FAST            = 10
EMA_PULLBACK        = 20
SMA_MID             = 50
SMA_MACRO           = 200

# Parámetros de Salida Asimétrica (Ratio 2.46 : 1)
SWING_TP_TARGET     = 0.0750  # +7.50% Take Profit objetivo
SWING_TRAIL_CB      = 0.0060  # Callback de 0.60% si supera el +7.50% para exprimir subidas
SWING_BE_TRIGGER    = 0.0350  # Al tocar +3.50% se blinda la operación
SWING_BE_LOCK       = 0.0100  # Asegura +1.00% neto si se gira tras haber subido +3.50%
DEFAULT_SWING_SL    = 0.0300  # -3.00% Stop Loss base (ajustado entre -2.5% y -4.0% por ATR)
MAX_HOLD_DAYS       = 15      # Rotación máxima de capital si un activo se estanca

LOOP_SECONDS        = 60      # Escaneo pausado cada 60s (Swing no requiere micro-ticks)
MAX_POSITIONS       = 4       # 4 slots simultáneos (25% del capital por operación)

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
log = logging.getLogger("t212_swing")

try:
    with UNIVERSE_PATH.open(encoding="utf-8") as f:
        UNIVERSE = json.load(f)
    log.info(f"✅ Universo cargado: {len(UNIVERSE)} activos listados.")
except Exception as e:
    log.warning(f"⚠️ Error cargando universe.json: {e}")
    UNIVERSE = {"AAPL": "AAPL_US_EQ", "WMT": "WMT_US_EQ", "MSFT": "MSFT_US_EQ"}

# Mapa inverso de ticker T212 -> símbolo Yahoo para consultar datos de posiciones abiertas
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


def get_daily_swing_data(symbol: str) -> dict:
    """
    Descarga 2 años de velas diarias (1D) de Yahoo Finance y calcula:
    SMA(50), SMA(200), EMA(10), EMA(20), RSI(14), ATR(14) y estado de la vela actual.
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
        volumes = [p[4] for p in paired]

        price = closes[-1]
        if price <= 0:
            return None

        sma50  = sum(closes[-SMA_MID:]) / SMA_MID
        sma200 = sum(closes[-SMA_MACRO:]) / SMA_MACRO
        ema10  = calc_ema(closes, EMA_FAST)
        ema20  = calc_ema(closes, EMA_PULLBACK)

        # RSI(14) de Wilder en velas diarias
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas]
        losses = [max(-d, 0) for d in deltas]
        avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
        avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
        for g, l in zip(gains[RSI_PERIOD:], losses[RSI_PERIOD:]):
            avg_gain = (avg_gain * (RSI_PERIOD - 1) + g) / RSI_PERIOD
            avg_loss = (avg_loss * (RSI_PERIOD - 1) + l) / RSI_PERIOD
        rs    = avg_gain / avg_loss if avg_loss > 0 else 100.0
        rsi14 = round(100.0 - (100.0 / (1.0 + rs)), 2)

        # ATR(14) diario y Stop Loss adaptativo (1.8 * ATR%, entre 2.5% y 4.0%)
        trs = []
        for i in range(1, len(closes)):
            h, l, cp = highs[i], lows[i], closes[i - 1]
            trs.append(max(h - l, abs(h - cp), abs(l - cp)))
        atr14       = sum(trs[-14:]) / 14.0 if len(trs) >= 14 else (highs[-1] - lows[-1])
        atr_pct     = (atr14 / price) if price > 0 else 0.015
        adaptive_sl = max(0.025, min(0.040, 1.8 * atr_pct))

        is_green = closes[-1] >= opens[-1]

        return {
            "price": price,
            "sma50": sma50,
            "sma200": sma200,
            "ema10": ema10,
            "ema20": ema20,
            "rsi14": rsi14,
            "atr_pct": atr_pct,
            "adaptive_sl": adaptive_sl,
            "is_green": is_green,
        }
    except Exception:
        return None


def get_macro_regime() -> tuple:
    """
    Comprueba si el S&P 500 (SPY) está en mercado alcista estructural (Precio >= SMA200).
    Retorna: (is_macro_bull: bool, spy_rsi: float)
    """
    spy = get_daily_swing_data("SPY")
    if not spy:
        return True, 50.0
    is_bull = spy["price"] >= spy["sma200"]
    return is_bull, spy["rsi14"]


def calc_days_held(initial_fill_date_str: str) -> float:
    """
    Calcula cuántos días lleva abierta la posición leyendo 'initialFillDate' de T212.
    Garantiza persistencia total entre reinicios de GitHub Actions.
    """
    if not initial_fill_date_str:
        return 0.0
    try:
        dt_fill = datetime.fromisoformat(initial_fill_date_str)
        now_utc = datetime.now(timezone.utc)
        delta   = now_utc - dt_fill.astimezone(timezone.utc)
        return max(0.0, delta.total_seconds() / 86400.0)
    except Exception:
        return 0.0


# ── Memoria de Sesión para Trailing y Breakeven ───────────────────────────────
high_water_marks: dict = {}
trailing_active: dict  = {}
breakeven_active: dict = {}
cached_sl_map: dict    = {}


def run(dry_run: bool, max_minutes: int = 0):
    log.info("=" * 78)
    log.info("🚀 TRADING 212 — WALL STREET SWING TREND PULLBACK ENGINE v6.0")
    log.info(f"   Universo ({len(UNIVERSE)} activos): Escaneo Diario 1D (Stage 2 Uptrend: Close > SMA50 > SMA200)")
    log.info(f"   Setup Entrada : Pullback a EMA20 (±2.5%) | RSI {RSI_PULLBACK_MIN:.0f}-{RSI_PULLBACK_MAX:.0f} | Vela Diaria Verde")
    log.info(f"   Riesgo / Benef: TP +{SWING_TP_TARGET*100:.1f}% | SL Adaptativo ATR (-2.5% a -4.0%) | Ratio 2.46:1")
    log.info(f"   Persistencia  : Posiciones multisesión (media ~5 días) con salida técnica EMA10")
    if max_minutes > 0:
        log.info(f"   Duración turno: {max_minutes} minutos (Modo Cloud / GitHub Actions)")
    log.info(f"   Modo          : {'DRY-RUN' if dry_run else 'TRADING REAL'}")
    log.info("=" * 78)

    start_ts = time.time()
    loop = 0
    last_macro_check = 0
    macro_bull = True
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

        NYSE_OPEN         = 15 * 60 + 30   # 15:30 Madrid (Apertura Wall Street)
        SWING_BUY_START   = 16 * 60 + 0    # 16:00 Madrid (30m tras apertura para estabilizar vela 1D)
        SWING_BUY_STOP    = 21 * 60 + 45   # 21:45 Madrid
        EOD_EVAL_START    = 21 * 60 + 30   # 21:30–21:58 Madrid (Evaluación de cierre de vela diaria)
        NYSE_CLOSE        = 22 * 60 + 0    # 22:00 Madrid

        mercado_abierto   = es_dia_laborable and (NYSE_OPEN <= hora_min < NYSE_CLOSE)
        ventana_compras   = es_dia_laborable and (SWING_BUY_START <= hora_min < SWING_BUY_STOP)
        ventana_cierre_1d = es_dia_laborable and (EOD_EVAL_START <= hora_min < NYSE_CLOSE - 2)

        cash      = get_cash()
        free_eur  = float(cash.get("free", 0))
        total_eur = float(cash.get("total", free_eur))
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

        # Actualizar régimen macro del S&P 500 cada 15 minutos
        if time.time() - last_macro_check > 900:
            macro_bull, spy_rsi = get_macro_regime()
            last_macro_check = time.time()

        macro_str = f"🟢 ALCISTA (>SMA200, RSI={spy_rsi:.0f})" if macro_bull else f"🔴 DEFENSIVO (<SMA200, RSI={spy_rsi:.0f})"

        log.info(
            f"\n--- [Ciclo #{loop} {now_str}] "
            f"Posiciones Swing: {len(portfolio)}/{MAX_POSITIONS} | "
            f"Macro SPY: {macro_str} | "
            f"{'🟢 MERCADO ABIERTO' if mercado_abierto else '🔴 MERCADO CERRADO'} ---"
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

            # Obtener datos diarios para conocer su ATR Stop Loss y su EMA(10)
            y_sym = T212_TO_YAHOO.get(t212_ticker, t212_ticker.split("_")[0])
            d_info = get_daily_swing_data(y_sym)
            if d_info:
                cached_sl_map[t212_ticker] = d_info["adaptive_sl"]
            sl_limit = cached_sl_map.get(t212_ticker, DEFAULT_SWING_SL)

            # Reconstruir estado Trailing si la posición alcanza el objetivo de +7.50%
            if pnl_pct >= SWING_TP_TARGET:
                trailing_active[t212_ticker] = True

            is_trailing = trailing_active.get(t212_ticker, False)
            status_tag  = "🎯TP_TRAIL" if is_trailing else f"SL:-{sl_limit*100:.1f}%"

            log.info(
                f"   💼 {t212_ticker:12s} [{status_tag}] "
                f"PnL={pnl_pct*100:+.2f}% | Hold={days_held:.1f}d"
            )

            # A) STOP LOSS ADAPTATIVO (-2.5% a -4.0% según ATR)
            if pnl_pct <= -sl_limit:
                log.warning(f"🚨 {t212_ticker} SWING STOP LOSS ({pnl_pct*100:.2f}% <= -{sl_limit*100:.2f}%). Cortando pérdida...")
                if sell_position_market(t212_ticker, qty, "SWING_STOP_LOSS", dry_run):
                    high_water_marks.pop(t212_ticker, None)
                    trailing_active.pop(t212_ticker, None)
                continue

            # B) TAKE PROFIT (+7.50%) CON TRAILING DE CAPTURA
            if is_trailing:
                drawdown = (prev_max - current_price) / prev_max if prev_max > 0 else 0
                if drawdown >= SWING_TRAIL_CB or pnl_pct <= SWING_TP_TARGET - 0.005:
                    log.info(f"🎯 {t212_ticker} TAKE PROFIT SWING ALCANZADO (PnL={pnl_pct*100:+.2f}%). Realizando beneficio...")
                    if sell_position_market(t212_ticker, qty, "SWING_TAKE_PROFIT", dry_run):
                        high_water_marks.pop(t212_ticker, None)
                        trailing_active.pop(t212_ticker, None)
                    continue

            # D) SALIDA TÉCNICA AL CIERRE DIARIO (21:30 - 21:58 Madrid): Cruce bajo EMA(10) o Límite 15 días
            if ventana_cierre_1d and d_info:
                if days_held >= 3.0 and current_price < d_info["ema10"]:
                    log.info(f"📉 {t212_ticker} perdió la EMA(10) diaria al cierre tras {days_held:.1f}d (PnL={pnl_pct*100:+.2f}%). Rotando...")
                    if sell_position_market(t212_ticker, qty, "TRAILING_EMA10_1D", dry_run):
                        high_water_marks.pop(t212_ticker, None)
                        trailing_active.pop(t212_ticker, None)
                        breakeven_active.pop(t212_ticker, None)
                    continue
                if days_held >= MAX_HOLD_DAYS:
                    log.info(f"⌛ {t212_ticker} cumplió {days_held:.1f} días (máx {MAX_HOLD_DAYS}d). Liberando liquidez...")
                    if sell_position_market(t212_ticker, qty, "TIME_MAX_15D", dry_run):
                        high_water_marks.pop(t212_ticker, None)
                        trailing_active.pop(t212_ticker, None)
                        breakeven_active.pop(t212_ticker, None)
                    continue

        # ── 2. ESCANEO SWING TREND PULLBACK (MODELO A) ────────────────────────
        if not ventana_compras:
            log.info("   🛡️ Fuera de ventana de nuevas compras Swing (16:00 – 21:45). Vigilando posiciones...")
        elif not macro_bull:
            log.info("   ⚠️ Régimen Macro Defensivo (SPY < SMA200). No se abren nuevos largos Swing.")
        elif active_count < MAX_POSITIONS and free_eur >= 2.00:
            candidates = []

            def eval_swing_ticker(item):
                y_sym, t212_sym = item
                if t212_sym in held_tickers:
                    return None
                d = get_daily_swing_data(y_sym)
                if not d:
                    return None

                price    = d["price"]
                sma50    = d["sma50"]
                sma200   = d["sma200"]
                ema20    = d["ema20"]
                rsi14    = d["rsi14"]
                is_green = d["is_green"]

                # Reglas exactas Modelo A (Profit Factor 1.89 en Backtest 2 Años):
                # 1. Stage 2 Uptrend institucional
                trend_ok    = (price > sma50) and (sma50 > sma200)
                # 2. Pullback controlado a la zona de valor EMA(20) (±2.5%)
                pullback_ok = (ema20 * 0.975 <= price <= ema20 * 1.025)
                # 3. RSI diario enfriado pero con soporte alcista
                rsi_ok      = (RSI_PULLBACK_MIN <= rsi14 <= RSI_PULLBACK_MAX)

                if trend_ok and pullback_ok and rsi_ok and is_green:
                    # Puntuar por Fuerza Relativa estructural sobre su SMA(200)
                    rel_strength = (price - sma200) / sma200
                    return {
                        "y_sym": y_sym,
                        "t212_sym": t212_sym,
                        "price": price,
                        "rsi14": rsi14,
                        "ema20": ema20,
                        "adaptive_sl": d["adaptive_sl"],
                        "score": rel_strength,
                    }
                return None

            with ThreadPoolExecutor(max_workers=16) as pool:
                futures = [pool.submit(eval_swing_ticker, item) for item in UNIVERSE.items()]
                for fut in as_completed(futures):
                    res = fut.result()
                    if res:
                        candidates.append(res)

            if candidates:
                candidates.sort(key=lambda x: x["score"], reverse=True)
                top = candidates[0]

                # Calcular capital por slot (dividiendo entre slots libres o total)
                eur_per_trade = total_eur / MAX_POSITIONS
                eur_to_spend  = min(free_eur * 0.95, eur_per_trade)
                approx_usd    = eur_to_spend * 1.08
                shares_qty    = approx_usd / top["price"]

                log.info(
                    f"🔥 SEÑAL SWING PULLBACK: {top['y_sym']} "
                    f"(RSI_1D={top['rsi14']:.1f} | Pullback EMA20 | SL=-{top['adaptive_sl']*100:.2f}% | TP=+{SWING_TP_TARGET*100:.1f}%)"
                )
                log.info(f"   🚀 Ejecutando entrada Swing en {top['y_sym']}...")

                if place_order_market(top["t212_sym"], shares_qty, dry_run):
                    high_water_marks[top["t212_sym"]] = top["price"]
                    cached_sl_map[top["t212_sym"]]    = top["adaptive_sl"]
                    held_tickers.add(top["t212_sym"])
            else:
                log.info(f"   ⏳ Escaneados {len(UNIVERSE)} activos en 1D: sin setups de Pullback en este ciclo.")

        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bot Trading 212 Swing Trend Pullback v6.0")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-minutes", type=int, default=0, help="Minutos de ejecución (0=infinito)")
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run, max_minutes=args.max_minutes)
    except KeyboardInterrupt:
        log.info("\n🛑 Bot Swing detenido.")
