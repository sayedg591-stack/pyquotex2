#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QXChart Server - Pro Merged Edition v6.6 (Exact 40 Candles Chart + Accurate RSI TMA + Live Countdown)
=============================================================================
✅ جلب 12 ساعة فقط من البيانات (نصف يوم)
✅ استبعاد الشمعة الحالية غير المكتملة عند التحميل الأولي
✅ انتظار فعلي (await) لسد الفجوات قبل المتابعة (Zero Race Condition)
✅ استراتيجية RSI TMA Bands دقيقة 100% في الخلفية
✅ الشارت المرسل يعرض 40 شمعة بالضبط مع الأسهم الاحترافية
✅ إرسال الإشارة بعد 3 ثوانٍ من إغلاق الشمعة (لضمان استقرار البيانات)
✅ قفل تداول صارم: لا يمكن إرسال إشارة جديدة حتى تُرسل نتيجة الإشارة السابقة
✅ نظام المسح الشامل: يراقب كل العملات ويرسل الإشارة لأول زوج يحقق الشروط
✅ حساب وقت الدخول بناءً على وقت الشمعة المغلقة + 2 دقيقة (دقيق 100% ولا يتأخر)
✅ نظام التحقق المزدوج (M1 & M2): ينتظر الدقيقة الأولى، إذا خسر ينتظر الدقيقة الثانية
✅ عداد تنازلي حي (Countdown) يُحدّث كل 5 ثوانٍ في رسالة منفصلة
✅ تقرير ملكي يُرسل تلقائياً كل 10 صفقات يوضح النتائج والنسبة المئوية واتجاه الصفقة
✅ سيرفر FastAPI + WebSockets + SQLite + Quotex
"""

import os
import sys
import asyncio
import threading
import time
import json
import random
import shutil
import io
import requests
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from contextlib import asynccontextmanager

# ============================================
# ✅ Chart Libraries Setup
# ============================================
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import mplfinance as mpf

# ============================================
# ✅ SSL Setup
# ============================================
try:
    import certifi
    os.environ['SSL_CERT_FILE'] = certifi.where()
    os.environ['WEBSOCKET_CLIENT_CA_BUNDLE'] = certifi.where()
except ImportError:
    pass

# ============================================
# ✅ Dependencies Check
# ============================================
try:
    from pyquotex.stable_api import Quotex
    from pyquotex.types import ReconnectPolicy
except ImportError as e:
    print(f"❌ Missing dependency: {e}")
    print("   pip install pyquotex")
    sys.exit(1)

try:
    import aiosqlite
except ImportError:
    print(f"❌ Missing dependency: aiosqlite")
    print("   pip install aiosqlite")
    sys.exit(1)

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
except ImportError:
    print("❌ Missing dependency: fastapi uvicorn")
    print("   pip install fastapi uvicorn websockets")
    sys.exit(1)


# ============================================
# 🎨 Colors
# ============================================
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'


# ============================================
# ⚙️ Settings
# ============================================
PERIOD = 1
PERIOD_SECONDS = 60
INITIAL_CANDLES = 500
MIN_CANDLES_THRESHOLD = 100

TELEGRAM_CANDLE_LIMIT = 40 
FETCH_DURATION_SECONDS = 43200  

# RSI TMA Settings
RSI_LENGTH = 2
HALF_LENGTH = 2
DEV_PERIOD = 100
DEVIATIONS = 0.7

SIGNAL_SEND_DELAY = 3.0
COUNTDOWN_UPDATE_INTERVAL = 5

forex_assets = [
    "USDARS_otc", "USDBDT_otc", "USDCOP_otc", "USDDZD_otc", "USDEGP_otc",
    "USDIDR_otc", "USDINR_otc", "USDMXN_otc", "USDNGN_otc", "USDPHP_otc",
    "USDPKR_otc", "USDZAR_otc",
]
ASSET_DISPLAY_MAP = forex_assets

STREAM_POLL_INTERVAL = 0.15
DB_WRITE_INTERVAL = 0.5
DB_PATH = "candles.db"
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000

TELEGRAM_BOT_TOKEN = None
TELEGRAM_CHAT_ID = None

ALL_ASSETS_LOADED = False
GLOBAL_LAST_SIGNAL_MINUTE = 0
SIGNAL_LOCK = asyncio.Lock()
PRINT_LOCK = asyncio.Lock()

TRADE_IN_PROGRESS = False


# ============================================
# 🏗️ Asset Class
# ============================================
class Asset:
    def __init__(self, symbol: str):
        self.symbol = symbol[:20]
        self.period = PERIOD
        self.price = 0.0
        self.candles: List[dict] = []
        self.candle_times: Set[int] = set()
        self.updates = 0
        self.streaming = False
        self.last_update_time = 0.0
        self.last_db_write = 0.0
        self.total_ticks = 0
        self.last_candle_time = 0
        self.missing_candles_filled = 0
        self.new_candles_added = 0
        self.history_loaded = False
        self.last_signaled_time = 0

    def digits(self):
        if self.price >= 1000: return 2
        elif self.price >= 10: return 3
        elif self.price > 0: return 5
        else: return 5


# ============================================
# 🧠 RSI TMA Signal Checker
# ============================================
def check_rsi_tma_signal(candles_list: List[dict]):
    if len(candles_list) < (DEV_PERIOD + 10):
        return False, False

    df = pd.DataFrame(candles_list)
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(window=RSI_LENGTH).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_LENGTH).mean()
    rs = gain / loss
    rsi = (100 - (100 / (1 + rs))).fillna(50)
    
    window = HALF_LENGTH + 1
    sma1 = rsi.rolling(window=window).mean()
    tma = sma1.rolling(window=window).mean().shift(HALF_LENGTH)
    
    std_dev = rsi.rolling(window=DEV_PERIOD).std().fillna(0)
    chUp = tma + (std_dev * DEVIATIONS)
    chDn = tma - (std_dev * DEVIATIONS)
    
    curr_rsi, prev_rsi = rsi.iloc[-1], rsi.iloc[-2]
    curr_chUp, prev_chUp = chUp.iloc[-1], chUp.iloc[-2]
    curr_chDn, prev_chDn = chDn.iloc[-1], chDn.iloc[-2]
    
    is_buy = (curr_rsi < curr_chDn) and (prev_rsi > prev_chDn)
    is_sell = (curr_rsi > curr_chUp) and (prev_rsi < prev_chUp)
    
    return bool(is_buy), bool(is_sell)


# ============================================
# 📈 Trade Result Tracking & Stats Board
# ============================================
SIGNAL_RESULTS: List[dict] = []
RESULTS_LOCK = asyncio.Lock()
STATS_BATCH_SIZE = 10

def _fancy_outcome(win: bool) -> str:
    return "➢   𝑾 𝑰 𝑵   ✓" if win else "➢   𝑳 𝑶 𝑺 𝑺   ✘"

def build_results_board(results: list) -> str:
    lines = []
    lines.append("╔═════════════════════╗")
    lines.append("             𒁂  𝑸𝑿 𝒁𝑬𝑹𝑶  𒁂")
    lines.append("╠═════════════════════╣")
    lines.append("            𒐬  𝑹𝑬𝑺𝑼𝑳𝑻𝑺  𒐬")
    lines.append("╠═════════════════════╣")
    lines.append("")
    for r in results:
        sym_disp = r['symbol'].replace('_otc', '-OTC').upper()
        dir_str = "𝐁𝐔𝐘" if r['direction'] == "BUY" else "𝐒𝐄𝐋𝐋"
        lines.append(f"   {sym_disp:<12} {dir_str} {_fancy_outcome(r['win'])}")
    wins = sum(1 for r in results if r['win'])
    losses = len(results) - wins
    accuracy = (wins / len(results)) * 100 if results else 0
    lines.append("")
    lines.append("╠═════════════════════╣")
    lines.append(f"      ✦  {wins} 𝑾𝑰𝑵   │   {losses} 𝑳𝑶𝑺𝑮  ✦")
    lines.append(f"      ✧  𝑨𝑪𝑪𝑼𝑹𝑨𝑪𝒀  ➢  {accuracy:.0f}%  ✧")
    lines.append("╚═════════════════════╝")
    return "\n".join(lines)


# ============================================
# 📝 Telegram Message Builders
# ============================================
def build_royal_signal_caption(symbol_display: str, direction: str, entry_time_str: str) -> str:
    dir_emoji = "🟩" if direction == "BUY" else "🟥"
    dir_str = "𝐁𝐔𝐘" if direction == "BUY" else "𝐒𝐄𝐋𝐋"
    return (
        "╔═════════════════════╗\n"
        "               💎  𝑸𝑿 𝒁𝑬𝑹𝑶  💎\n"
        "╠═════════════════════╣\n"
        "                  👑  𝕍  𝕀  ℙ  👑\n"
        f"   💱  𝑨𝑺𝑺𝑶𝑻  ➠ {symbol_display}\n"
        "   ⏳  𝑭𝑹𝑨𝑴𝑶 ➠ 1M\n"
        f"   🕰  𝑬𝑵𝑻𝑹𝒀  ➠ {entry_time_str}\n"
        f"   {dir_emoji}  𝑫𝑰𝑹𝑶𝑪𝑻𝑰𝑶𝑵 ➠ {dir_str}\n"
        "\n"
        "╠═════════════════════╣\n"
        "         ❰ 👑 𝑹𝑶𝒀𝑨𝑳 𝑮𝑶𝑳𝑫 👑 ❱\n"
        "╚═════════════════════╝"
    )

def build_countdown_message(phase: str, seconds_left: int) -> str:
    if phase == "ENTRY":
        return f"⏳ 𝑬𝑵𝑻𝑹𝒀    {max(0, seconds_left):2d}s"
    elif phase == "EXPIRY":
        return f"⏱️ 𝑬𝑿𝑷𝑰𝑹𝒀  {max(0, seconds_left):2d}s"
    elif phase == "MARTINGALE":
        return f"⏱️ 𝑴𝑨𝑹𝑻𝑰𝑵𝑮𝑨𝑳𝑶  {max(0, seconds_left):2d}s"
    return ""

def build_result_message(symbol_display: str, is_win: bool) -> str:
    if is_win:
        return (
            "╔═══════════════════╗\n"
            f"  {symbol_display}  ➜ 💎 𝑾𝑰𝑵 💎\n"
            "╚═══════════════════╝"
        )
    else:
        return (
            "╔═══════════════════╗\n"
            f"  {symbol_display}  ➜ 💢 𝑳𝑶𝑺𝑺 💢\n"
            "╚═══════════════════╝"
        )


# ============================================
# 📡 Telegram API Helpers
# ============================================
def send_telegram_message(text: str, bot_token: str, chat_id: str) -> dict:
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        response = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
        return response.json()
    except Exception as e:
        print(f"{Colors.RED}❌ Telegram message error: {e}{Colors.RESET}")
        return {"ok": False, "description": str(e)}

def edit_telegram_message(message_id: int, text: str, bot_token: str, chat_id: str, max_retries: int = 3) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=15)
            if response.status_code == 200:
                return True
            else:
                err_data = response.json()
                if "message is not modified" in err_data.get("description", "").lower():
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False

async def safe_edit_message(msg_id: int, text: str):
    try:
        await asyncio.to_thread(edit_telegram_message, msg_id, text, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    except Exception:
        pass


# ============================================
# ⏳ Trade Verification & Live Countdown Engine (M1 & M2)
# ============================================
async def _wait_until(target_ts: float):
    delay = target_ts - time.time()
    if delay > 0:
        await asyncio.sleep(delay)

async def _find_candle(asset: "Asset", candle_time: int, retries: int = 30, retry_delay: float = 1.0):
    for _ in range(retries):
        for c in reversed(asset.candles):
            if c['time'] == candle_time:
                return c
            if c['time'] < candle_time:
                break
        await asyncio.sleep(retry_delay)
    return None

async def fetch_specific_candle_from_api(asset: "Asset", candle_time: int) -> Optional[dict]:
    try:
        res = await asyncio.wait_for(CLIENT.get_candles(asset.symbol, time.time(), 300, PERIOD_SECONDS), timeout=10)
        if res:
            formatted = _format_candles(res)
            for c in formatted:
                if c['time'] == candle_time:
                    return c
    except Exception:
        pass
    return None

async def verify_trade_result_and_countdown(asset: "Asset", entry_candle_time: int, is_buy: bool, countdown_msg_id: Optional[int] = None):
    global TRADE_IN_PROGRESS
    try:
        # --- Phase 1: Entry Countdown ---
        print(f"{Colors.CYAN}[VERIFY] Phase 1: Waiting for Entry Time ({datetime.fromtimestamp(entry_candle_time).strftime('%H:%M:%S')})...{Colors.RESET}")
        next_update_time = time.time()
        while time.time() < entry_candle_time:
            try:
                now = time.time()
                if now >= next_update_time and countdown_msg_id is not None:
                    entry_left = int(entry_candle_time - now)
                    text = build_countdown_message("ENTRY", entry_left)
                    asyncio.create_task(safe_edit_message(countdown_msg_id, text))
                    next_update_time = now + COUNTDOWN_UPDATE_INTERVAL
            except Exception: pass
            await asyncio.sleep(1)

        entry_candle = await _find_candle(asset, entry_candle_time, retries=30)
        if entry_candle is None:
            entry_candle = await fetch_specific_candle_from_api(asset, entry_candle_time)
            
        if entry_candle is None:
            if countdown_msg_id: asyncio.create_task(safe_edit_message(countdown_msg_id, "⚠️ Skip (Stream Lag)"))
            print(f"{Colors.RED}⚠️ {asset.symbol}: Entry candle {entry_candle_time} never appeared — result skipped.{Colors.RESET}")
            return
            
        entry_price = entry_candle['open']
        exit1_due = entry_candle_time + PERIOD_SECONDS
        
        # --- Phase 2: 1st Expiry Countdown (M1) ---
        print(f"{Colors.CYAN}[VERIFY] Phase 2: Waiting for 1st Candle Close ({datetime.fromtimestamp(exit1_due).strftime('%H:%M:%S')})...{Colors.RESET}")
        next_update_time = time.time()
        while time.time() < exit1_due:
            try:
                now = time.time()
                if now >= next_update_time and countdown_msg_id is not None:
                    expiry_left = int(exit1_due - now)
                    text = build_countdown_message("EXPIRY", expiry_left)
                    asyncio.create_task(safe_edit_message(countdown_msg_id, text))
                    next_update_time = now + COUNTDOWN_UPDATE_INTERVAL
            except Exception: pass
            await asyncio.sleep(1)

        closed_candle1 = await _find_candle(asset, entry_candle_time, retries=30)
        if closed_candle1 is None:
            closed_candle1 = await fetch_specific_candle_from_api(asset, entry_candle_time)
        if closed_candle1 is None:
            print(f"{Colors.RED}❌ CRITICAL: 1st close candle missing. Trade skipped.{Colors.RESET}")
            return
        
        exit1_price = closed_candle1['close']
        win1 = (exit1_price > entry_price) if is_buy else (exit1_price < entry_price)

        if win1:
            win = True
            print(f"{Colors.GREEN}[VERIFY] ✅ WIN on 1st Candle. Entry: {entry_price}, Close: {exit1_price}{Colors.RESET}")
        else:
            print(f"{Colors.RED}[VERIFY] 📉 LOSS on 1st Candle. Entry: {entry_price}, Close: {exit1_price}. Activating Martingale (M2)...{Colors.RESET}")
            
            # --- Phase 3: Martingale (2nd Candle / M2) ---
            entry2_candle_time = entry_candle_time + PERIOD_SECONDS
            entry2_candle = await _find_candle(asset, entry2_candle_time, retries=30)
            if entry2_candle is None:
                entry2_candle = await fetch_specific_candle_from_api(asset, entry2_candle_time)
            if entry2_candle is None:
                print(f"{Colors.RED}❌ CRITICAL: 2nd entry candle missing. Martingale skipped.{Colors.RESET}")
                return
            entry2_price = entry2_candle['open']

            exit2_due = entry2_candle_time + PERIOD_SECONDS
            print(f"{Colors.CYAN}[VERIFY] Phase 3: Waiting for 2nd Candle (Martingale) Close ({datetime.fromtimestamp(exit2_due).strftime('%H:%M:%S')})...{Colors.RESET}")
            
            next_update_time = time.time()
            while time.time() < exit2_due:
                try:
                    now = time.time()
                    if now >= next_update_time and countdown_msg_id is not None:
                        expiry_left = int(exit2_due - now)
                        text = build_countdown_message("MARTINGALE", expiry_left)
                        asyncio.create_task(safe_edit_message(countdown_msg_id, text))
                        next_update_time = now + COUNTDOWN_UPDATE_INTERVAL
                except Exception: pass
                await asyncio.sleep(1)

            closed_candle2 = await _find_candle(asset, entry2_candle_time, retries=30)
            if closed_candle2 is None:
                closed_candle2 = await fetch_specific_candle_from_api(asset, entry2_candle_time)
            if closed_candle2 is None:
                print(f"{Colors.RED}❌ CRITICAL: 2nd close candle missing. Martingale skipped.{Colors.RESET}")
                return
            exit2_price = closed_candle2['close']

            win2 = (exit2_price > entry2_price) if is_buy else (exit2_price < entry2_price)
            win = win2
            if win2:
                print(f"{Colors.GREEN}[VERIFY] ✅ WIN on 2nd Candle (Martingale). Entry: {entry2_price}, Close: {exit2_price}{Colors.RESET}")
            else:
                print(f"{Colors.RED}[VERIFY] ❌ LOSS on 2nd Candle (Martingale). Entry: {entry2_price}, Close: {exit2_price}{Colors.RESET}")

        # --- Phase 4: Final Result Update ---
        sym_disp = asset.symbol.replace('_otc', '-OTC').upper()
        final_text = build_result_message(sym_disp, win)
        if countdown_msg_id is not None:
            asyncio.create_task(safe_edit_message(countdown_msg_id, final_text))

        async with RESULTS_LOCK:
            SIGNAL_RESULTS.append({
                "symbol": asset.symbol,
                "direction": "BUY" if is_buy else "SELL",
                "win": win,
                "entry": entry_price,
                "exit": exit1_price if win1 else exit2_price,
            })
            if len(SIGNAL_RESULTS) >= STATS_BATCH_SIZE:
                batch = SIGNAL_RESULTS[-STATS_BATCH_SIZE:]
                board = build_results_board(batch)
                if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                    await asyncio.to_thread(send_telegram_message, board, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
                SIGNAL_RESULTS.clear()
                
    except Exception as e:
        print(f"{Colors.RED}❌ Trade Verification Error: {e}{Colors.RESET}")
    finally:
        TRADE_IN_PROGRESS = False
        print(f"{Colors.CYAN}🔓 Trade lock released. Ready for new signals.{Colors.RESET}")


# ============================================
# 📸 Telegram Chart Generator
# ============================================
def generate_and_send_chart(candles: list, symbol: str, bot_token: str, chat_id: str, is_buy: bool = False, is_sell: bool = False, entry_time: int = None):
    if not candles or not isinstance(candles, list) or len(candles) < 10:
        return

    try:
        df = pd.DataFrame(candles)
        df = df.dropna(subset=['time', 'open', 'high', 'low', 'close'])
        if len(df) < 10: return

        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)
        df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)

        real_len = len(df)
        pad_count = int(real_len * 0.15)
        if pad_count > 0:
            last_time = df.index[-1]
            pad_index = pd.date_range(start=last_time + timedelta(minutes=1), periods=pad_count, freq='1min')
            pad_df = pd.DataFrame(np.nan, index=pad_index, columns=df.columns, dtype=float)
            df = pd.concat([df, pad_df])

        price_min = df['low'].min()
        price_max = df['high'].max()
        price_range = price_max - price_min
        padding_y = price_range * 0.05

        mc = mpf.make_marketcolors(up='#279e07', down='#bb1423', edge={'up':'#279e07', 'down':'#bb1423'}, wick={'up':'#279e07', 'down':'#bb1423'}, volume='in')
        s = mpf.make_mpf_style(marketcolors=mc, facecolor='#000000', edgecolor='#000000', figcolor='#000000', gridcolor='#404040', gridstyle='--',
            rc={'axes.labelcolor': '#00BFFF', 'xtick.color': '#00BFFF', 'ytick.color': '#00BFFF', 'axes.edgecolor': '#333333', 'figure.facecolor': '#000000', 'axes.facecolor': '#000000', 'grid.linewidth': 0.3})

        buf = io.BytesIO()
        fig, axes = mpf.plot(df, type='candle', style=s, volume=False, ylabel='Price', figratio=(10, 6), figscale=1.5, tight_layout=True, returnfig=True)

        ax = axes[0]
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position('right')
        
        for spine in ['right', 'left', 'top', 'bottom']:
            ax.spines[spine].set_visible(True)
            ax.spines[spine].set_color('#00BFFF')
            ax.spines[spine].set_linewidth(1.5)

        ax.set_ylim(price_min - padding_y, price_max + padding_y)

        if is_buy or is_sell:
            x_last = real_len - 1
            color = '#FF3B3B' if is_sell else '#00E676'

            if is_sell:
                y_wick = df['high'].iloc[x_last]
                y_marker = y_wick + padding_y * 0.30
                ax.plot([x_last, x_last], [y_wick, y_marker - padding_y * 0.03], color=color, lw=1.4, zorder=99, solid_capstyle='round')
                ax.plot([x_last], [y_marker], marker='v', markersize=20, color=color, alpha=0.15, zorder=98, markeredgewidth=0)
                ax.plot([x_last], [y_marker], marker='v', markersize=10.5, color=color, markeredgecolor='#FFFFFF', markeredgewidth=0.7, zorder=100)
            else:
                y_wick = df['low'].iloc[x_last]
                y_marker = y_wick - padding_y * 0.30
                ax.plot([x_last, x_last], [y_wick, y_marker + padding_y * 0.03], color=color, lw=1.4, zorder=99, solid_capstyle='round')
                ax.plot([x_last], [y_marker], marker='^', markersize=20, color=color, alpha=0.15, zorder=98, markeredgewidth=0)
                ax.plot([x_last], [y_marker], marker='^', markersize=10.5, color=color, markeredgecolor='#FFFFFF', markeredgewidth=0.7, zorder=100)

        ax.set_title(f"{symbol} - Signal Chart", color='#00BFFF', fontsize=14, pad=20)
        ax.yaxis.label.set_color('#00BFFF')
        ax.tick_params(axis='y', colors='#00BFFF', labelsize=8)
        ax.tick_params(axis='x', colors='#00BFFF', labelsize=7, length=3, width=0.5)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        for label in ax.get_xticklabels():
            label.set_rotation(0); label.set_fontsize(7)

        fig.savefig(buf, dpi=300, facecolor=fig.get_facecolor(), bbox_inches='tight')
        buf.seek(0)
        plt.close(fig)

        direction_str = "BUY" if is_buy else "SELL"
        symbol_display = symbol.replace('_otc', '-OTC').upper()
        entry_time_str = datetime.fromtimestamp(entry_time).strftime('%H:%M:%S') if entry_time else datetime.now().strftime('%H:%M:%S')
        
        caption = build_royal_signal_caption(symbol_display, direction_str, entry_time_str)
        
        url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        files = {'photo': ('chart.png', buf, 'image/png')}
        data = {'chat_id': chat_id, 'caption': caption}

        response = requests.post(url, files=files, data=data, timeout=15)
        if response.status_code == 200:
            print(f"{Colors.GREEN}✅ Telegram Chart sent for {symbol}!{Colors.RESET}")
    except Exception as e:
        print(f"{Colors.RED}❌ Chart Error for {symbol}: {e}{Colors.RESET}")

async def trigger_telegram_chart(asset: Asset, closed_candles: list, is_buy: bool = False, is_sell: bool = False, entry_time: int = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    candles_to_send = closed_candles[-TELEGRAM_CANDLE_LIMIT:].copy()
    await asyncio.to_thread(generate_and_send_chart, candles_to_send, asset.symbol, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, is_buy, is_sell, entry_time)


# ============================================
# 💾 Database Manager
# ============================================
class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def connect(self):
        if self.db: return
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")
        await self.db.execute("PRAGMA cache_size=-64000")
        await self.db.execute("PRAGMA temp_store=MEMORY")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.commit()

    def _table_name(self, symbol: str) -> str:
        return f'candle_{symbol.replace("-", "_").replace(".", "_").replace(" ", "_")}'

    async def init_table(self, symbol: str):
        table = self._table_name(symbol)
        await self.db.execute(f'''CREATE TABLE IF NOT EXISTS "{table}" (time INTEGER PRIMARY KEY, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume INTEGER DEFAULT 0)''')
        await self.db.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_time" ON "{table}"(time DESC)')
        await self.db.commit()

    async def upsert_candle(self, symbol: str, candle: dict):
        if not self.db: return
        table = self._table_name(symbol)
        async with self._write_lock:
            await self.db.execute(f'''INSERT INTO "{table}" (time, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(time) DO UPDATE SET high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume''', 
                                  (candle['time'], candle['open'], candle['high'], candle['low'], candle['close'], candle['volume']))

    async def upsert_candles_batch(self, symbol: str, candles: list):
        if not self.db: return
        table = self._table_name(symbol)
        data = [(c['time'], c['open'], c['high'], c['low'], c['close'], c['volume']) for c in candles]
        async with self._write_lock:
            await self.db.executemany(f'''INSERT INTO "{table}" (time, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(time) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume''', data)
            await self.db.commit()

    async def get_candles(self, symbol: str, limit: int = 500) -> list:
        if not self.db: return []
        table = self._table_name(symbol)
        cursor = await self.db.execute(f'SELECT * FROM "{table}" ORDER BY time DESC LIMIT ?', (limit,))
        rows = await cursor.fetchall()
        return [{'time': r['time'], 'open': r['open'], 'high': r['high'], 'low': r['low'], 'close': r['close'], 'volume': r['volume']} for r in reversed(rows)]

    async def close(self):
        if self.db: await self.db.close(); self.db = None

db_manager = DatabaseManager(DB_PATH)


# ============================================
# 📡 WebSocket Connection Manager
# ============================================
class ConnectionManager:
    def __init__(self):
        self.tick_subscribers: Dict[str, Set[WebSocket]] = {}
        self.candle_subscribers: Dict[str, Set[WebSocket]] = {}
        self.all_subscribers: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def _add(self, store: dict, symbol: str, ws: WebSocket):
        async with self._lock:
            if symbol not in store: store[symbol] = set()
            store[symbol].add(ws)

    async def _remove(self, store: dict, symbol: str, ws: WebSocket):
        async with self._lock:
            if symbol in store:
                store[symbol].discard(ws)
                if not store[symbol]: del store[symbol]

    async def accept_tick(self, symbol: str, ws: WebSocket):
        await ws.accept(); await self._add(self.tick_subscribers, symbol, ws); self.all_subscribers.add(ws)

    async def disconnect_tick(self, symbol: str, ws: WebSocket):
        await self._remove(self.tick_subscribers, symbol, ws); self.all_subscribers.discard(ws)

    async def accept_candle(self, symbol: str, ws: WebSocket):
        await ws.accept(); await self._add(self.candle_subscribers, symbol, ws); self.all_subscribers.add(ws)

    async def disconnect_candle(self, symbol: str, ws: WebSocket):
        await self._remove(self.candle_subscribers, symbol, ws); self.all_subscribers.discard(ws)

    async def broadcast_tick(self, symbol: str, data: dict):
        subs = self.tick_subscribers.get(symbol)
        if not subs: return
        tasks = [ws.send_json(data) for ws in subs]
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)

    async def broadcast_candle(self, symbol: str, data: dict):
        subs = self.candle_subscribers.get(symbol)
        if not subs: return
        tasks = [ws.send_json(data) for ws in subs]
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)

connection_manager = ConnectionManager()


# ============================================
# 🧹 Session Manager & Smart Reconnect
# ============================================
SESSION_FILE = Path("session.json")
SESSION_STATE_FILE = Path("session_state.json")
EMAIL_FILE = Path("saved_email.txt")

class SessionManager:
    def __init__(self): self.state = self._load_state()

    def _load_state(self) -> dict:
        try:
            if SESSION_STATE_FILE.exists(): return json.loads(SESSION_STATE_FILE.read_text())
        except: pass
        return {"last_login": 0, "last_success": False, "failed_auth_count": 0, "email": ""}

    def _save_state(self):
        try: SESSION_STATE_FILE.write_text(json.dumps(self.state, indent=2))
        except: pass

    def should_force_fresh(self) -> bool:
        if not self.state.get("last_success", False): return True
        if self.state.get("failed_auth_count", 0) >= 2: return True
        if time.time() - self.state.get("last_login", 0) > 7 * 24 * 3600: return True
        return False

    def record_success(self, email: str):
        self.state.update({"last_login": time.time(), "last_success": True, "failed_auth_count": 0, "email": email})
        self._save_state(); EMAIL_FILE.write_text(email)

    def record_failure(self, error: str):
        self.state.update({"last_login": time.time(), "last_success": False, "failed_auth_count": self.state.get("failed_auth_count", 0) + 1})
        self._save_state()

    def get_saved_email(self) -> str:
        email = self.state.get("email", "")
        if email: return email
        if EMAIL_FILE.exists(): return EMAIL_FILE.read_text().strip()
        return ""

    @staticmethod
    def delete_session_file():
        if SESSION_FILE.exists():
            try: SESSION_FILE.unlink(); return True
            except: pass
        return False

    @staticmethod
    def delete_browser_dir():
        browser_dir = Path("browser")
        if browser_dir.exists():
            try: shutil.rmtree(browser_dir, ignore_errors=True); return True
            except: pass
        return False

session_manager = SessionManager()

# ============================================
# 🌍 Global State
# ============================================
ASYNC_LOOP = None
CLIENT = None
EMAIL = None
PASSWORD = None
CONNECTION_ALIVE = False
ALL_STREAMING_ASSETS: List[Asset] = []
ASSET_BY_SYMBOL: Dict[str, Asset] = {}
LAST_HEALTH_CHECK = 0
HEALTH_CHECK_INTERVAL = 15

def start_async_engine():
    global ASYNC_LOOP
    ASYNC_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(ASYNC_LOOP)
    ASYNC_LOOP.run_forever()

def _is_authorization_error(reason: str) -> bool:
    if not reason: return False
    r = str(reason).lower()
    return any(k in r for k in ["authorization", "reject", "auth", "401", "403", "unauthorized", "invalid credentials", "session expired"])


# ============================================
# 🔐 Login & Smart Auto-Reconnect
# ============================================
async def connect_quotex(email, password, force_fresh=False, max_attempts=3):
    global CLIENT, CONNECTION_ALIVE, EMAIL, PASSWORD
    EMAIL, PASSWORD = email, password

    for attempt in range(1, max_attempts + 1):
        try:
            if CLIENT:
                try: await CLIENT.close(); await asyncio.sleep(0.5)
                except: pass
                CLIENT = None

            if force_fresh or attempt > 1:
                session_manager.delete_session_file()
                session_manager.delete_browser_dir()

            print(f"  [{attempt}/{max_attempts}] Connecting to Quotex...", end=" ", flush=True)
            reconnect_policy = ReconnectPolicy(enabled=True, max_attempts=0, base_delay=1.0, max_delay=30.0, jitter=0.1, stale_timeout=60.0)
            CLIENT = Quotex(email=email, password=password, host="qxbroker.com", lang="en", reconnect_policy=reconnect_policy)

            check, reason = await CLIENT.connect()
            if check:
                print(f"{Colors.GREEN}SUCCESS!{Colors.RESET}")
                try: await CLIENT.change_account("PRACTICE"); await asyncio.sleep(0.5)
                except: pass
                CONNECTION_ALIVE = True
                session_manager.record_success(email)
                return True
            else:
                error_msg = str(reason) if reason else "Unknown error"
                print(f"{Colors.RED}Failed: {error_msg[:60]}{Colors.RESET}")
                if _is_authorization_error(error_msg): force_fresh = True
                session_manager.record_failure(error_msg)
        except Exception as e:
            print(f"{Colors.RED}Error: {str(e)[:50]}{Colors.RESET}")
            session_manager.record_failure(str(e))

        if attempt < max_attempts: await asyncio.sleep(5 * attempt)

    CONNECTION_ALIVE = False
    return False

async def health_monitor():
    global LAST_HEALTH_CHECK
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        now = time.time()
        LAST_HEALTH_CHECK = now
        if not CONNECTION_ALIVE or CLIENT is None: continue
        
        stale_assets = [a for a in ALL_STREAMING_ASSETS if a.updates > 0 and (now - a.last_update_time > 20)]
        if stale_assets:
            for asset in stale_assets:
                asset.streaming = False
                asyncio.create_task(realtime_stream(asset))

async def auto_reconnect():
    global CLIENT, CONNECTION_ALIVE
    while True:
        await asyncio.sleep(30)
        if not CONNECTION_ALIVE or CLIENT is None:
            print(f"\n{Colors.YELLOW}🔄 Connection lost. Smart Reconnecting...{Colors.RESET}")
            email = session_manager.get_saved_email()
            if email and PASSWORD:
                success = await connect_quotex(email, PASSWORD, force_fresh=True, max_attempts=3)
                if success:
                    print(f"{Colors.GREEN}✅ Reconnected successfully!{Colors.RESET}")
                    for asset in ALL_STREAMING_ASSETS:
                        try:
                            await CLIENT.start_candles_stream(asset.symbol, PERIOD_SECONDS)
                            await asyncio.sleep(0.1)
                        except: pass


# ============================================
# 📊 Fetch Candles & Format
# ============================================
def _format_candles(raw_candles):
    formatted = []
    for c in raw_candles:
        if not isinstance(c, dict): continue
        try:
            ts = int(float(c.get("time", c.get("timestamp", 0))))
            aligned = (ts // PERIOD_SECONDS) * PERIOD_SECONDS
            o = float(c.get("open", 0))
            h = float(c.get("high", c.get("max", 0)))
            l = float(c.get("low", c.get("min", 0)))
            cl = float(c.get("close", 0))
            if o > 0 and h > 0 and l > 0 and cl > 0:
                formatted.append({
                    'time': aligned, 'open': o, 'high': h, 'low': l, 'close': cl, 
                    'volume': random.randint(50, 200)
                })
        except Exception: continue
    
    seen = set()
    unique = []
    for c in formatted:
        if c['time'] not in seen:
            seen.add(c['time'])
            unique.append(c)
    unique.sort(key=lambda x: x['time'])
    return unique


async def fetch_candles_once(asset: Asset):
    internal_name = asset.symbol
    candles = []

    if hasattr(CLIENT, 'get_candles_deep'):
        try:
            res = await asyncio.wait_for(
                CLIENT.get_candles_deep(internal_name, amount_of_seconds=FETCH_DURATION_SECONDS, period=PERIOD_SECONDS), 
                timeout=60
            )
            if res and len(res) > 0: candles = res
        except asyncio.TimeoutError:
            print(f"{Colors.YELLOW}⚠️ {asset.symbol}: Deep fetch timed out.{Colors.RESET}")
        except Exception:
            pass

    if not candles and hasattr(CLIENT, 'get_historical_candles'):
        try:
            res = await asyncio.wait_for(
                CLIENT.get_historical_candles(internal_name, amount_of_seconds=FETCH_DURATION_SECONDS, period=PERIOD_SECONDS), 
                timeout=60
            )
            if res and len(res) > 0: candles = res
        except: pass

    if not candles and hasattr(CLIENT, 'get_candles'):
        try:
            res = await asyncio.wait_for(
                CLIENT.get_candles(internal_name, time.time(), FETCH_DURATION_SECONDS, PERIOD_SECONDS), 
                timeout=60
            )
            if res and len(res) > 0: candles = res
        except: pass

    if candles:
        formatted = _format_candles(candles)
        
        current_time = int(time.time())
        if formatted:
            last_candle = formatted[-1]
            candle_end_time = last_candle['time'] + PERIOD_SECONDS
            if current_time < candle_end_time:
                formatted = formatted[:-1]
                print(f"{Colors.YELLOW}⚠️ {asset.symbol}: Excluded incomplete current candle{Colors.RESET}")
        
        unique = formatted[-INITIAL_CANDLES:]
        asset.candle_times = {c['time'] for c in unique}
        return unique
    
    return []


async def fetch_candles_with_retry(asset: Asset, max_retries=2):
    for attempt in range(1, max_retries + 1):
        candles = await fetch_candles_once(asset)
        if len(candles) >= MIN_CANDLES_THRESHOLD:
            asset.candles = candles
            if candles:
                asset.price = candles[-1]['close']
                asset.last_candle_time = candles[-1]['time']
            await db_manager.upsert_candles_batch(asset.symbol, candles)
            asset.history_loaded = True 
            return len(candles), attempt
        if attempt < max_retries: await asyncio.sleep(1) 
    
    asset.candles = candles if 'candles' in locals() else []
    if asset.candles:
        asset.price = asset.candles[-1]['close']
        asset.last_candle_time = asset.candles[-1]['time']
        await db_manager.upsert_candles_batch(asset.symbol, asset.candles)
        asset.history_loaded = True
    return len(asset.candles), max_retries


def add_candle_to_asset(asset: Asset, candle: dict) -> bool:
    if candle['time'] in asset.candle_times:
        for i, existing in enumerate(asset.candles):
            if existing['time'] == candle['time']:
                asset.candles[i] = candle
                return False
    
    asset.candles.append(candle)
    asset.candle_times.add(candle['time'])
    asset.new_candles_added += 1
    
    if len(asset.candles) > INITIAL_CANDLES:
        oldest = asset.candles.pop(0)
        asset.candle_times.discard(oldest['time'])
    return True


# ============================================
# 🔥 سد الثغرات (Gap Fill)
# ============================================
async def fetch_and_fill_missing_candles(asset: Asset, missing_count: int):
    try:
        if not asset.candles: return 0
            
        last_time = asset.candles[-1]['time']
        missing_duration = (missing_count + 3) * PERIOD_SECONDS
        candles = []
        
        if hasattr(CLIENT, 'get_candles'):
            try:
                candles = await asyncio.wait_for(
                    CLIENT.get_candles(asset.symbol, time.time(), missing_duration, PERIOD_SECONDS), timeout=15
                )
            except: pass
            
        if not candles and hasattr(CLIENT, 'get_historical_candles'):
            try:
                candles = await asyncio.wait_for(
                    CLIENT.get_historical_candles(asset.symbol, amount_of_seconds=missing_duration, period=PERIOD_SECONDS), timeout=15
                )
            except: pass

        if candles and len(candles) > 0:
            formatted = _format_candles(candles)
            new_count = 0
            
            for candle in formatted:
                if candle['time'] > last_time and candle['time'] not in asset.candle_times:
                    asset.candles.append(candle)
                    asset.candle_times.add(candle['time'])
                    new_count += 1
            
            asset.candles.sort(key=lambda x: x['time'])
            if len(asset.candles) > INITIAL_CANDLES:
                asset.candles = asset.candles[-INITIAL_CANDLES:]
                asset.candle_times = {c['time'] for c in asset.candles}
            
            if new_count > 0:
                await db_manager.upsert_candles_batch(asset.symbol, formatted)
            return new_count
    except Exception: pass
    return 0


async def _fill_gap_background(asset: Asset, missing_count: int):
    fetched = await fetch_and_fill_missing_candles(asset, missing_count)
    if fetched > 0:
        asset.missing_candles_filled += fetched
        if missing_count > 1:
            print(f"{Colors.GREEN}✓ {asset.symbol}: Filled {fetched} missing candles silently.{Colors.RESET}")


# ============================================
# 📡 Realtime Stream & Signal Generation
# ============================================
async def process_signal_delivery_and_verification(asset: Asset, closed_candles: list, is_buy: bool, is_sell: bool, entry_candle_time: int):
    global TRADE_IN_PROGRESS
    try:
        await asyncio.sleep(SIGNAL_SEND_DELAY)
        await trigger_telegram_chart(asset, closed_candles, is_buy, is_sell, entry_candle_time)
        await asyncio.sleep(0.5)
        
        entry_left = max(0, int(entry_candle_time - time.time()))
        initial_countdown_text = build_countdown_message("ENTRY", entry_left)
        countdown_response = await asyncio.to_thread(send_telegram_message, initial_countdown_text, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        
        countdown_msg_id = None
        if countdown_response.get("ok"):
            countdown_msg_id = countdown_response["result"]["message_id"]
            print(f"{Colors.GREEN}✅ Countdown message sent (ID: {countdown_msg_id}).{Colors.RESET}")
        else:
            print(f"{Colors.RED}❌ Failed to send countdown message.{Colors.RESET}")
        
        await verify_trade_result_and_countdown(asset, entry_candle_time, is_buy, countdown_msg_id)
        
    except Exception as e:
        print(f"{Colors.RED}❌ Signal Processing Error: {e}{Colors.RESET}")
        TRADE_IN_PROGRESS = False


async def _maybe_check_and_fire_signal(asset: Asset):
    global GLOBAL_LAST_SIGNAL_MINUTE, TRADE_IN_PROGRESS

    if TRADE_IN_PROGRESS:
        return

    if not (ALL_ASSETS_LOADED and asset.history_loaded and len(asset.candles) >= (DEV_PERIOD + 11)):
        return

    now_aligned = (int(time.time()) // PERIOD_SECONDS) * PERIOD_SECONDS

    if asset.candles[-1]['time'] >= now_aligned:
        closed_candles = asset.candles[:-1]
    else:
        closed_candles = asset.candles

    if len(closed_candles) < (DEV_PERIOD + 10):
        return

    is_buy_sig, is_sell_sig = check_rsi_tma_signal(closed_candles)
    if not (is_buy_sig or is_sell_sig):
        return

    last_closed_time = closed_candles[-1]['time']
    if asset.last_signaled_time == last_closed_time:
        return
    
    current_minute = last_closed_time // 60
    
    async with SIGNAL_LOCK:
        if current_minute == GLOBAL_LAST_SIGNAL_MINUTE:
            return 
        
        GLOBAL_LAST_SIGNAL_MINUTE = current_minute
        asset.last_signaled_time = last_closed_time

    signal_type = "BUY" if is_buy_sig else "SELL"
    entry_candle_time = last_closed_time + (2 * PERIOD_SECONDS)
    
    while entry_candle_time <= time.time():
        entry_candle_time += PERIOD_SECONDS
    
    print(f"{Colors.GREEN if is_buy_sig else Colors.RED}🚨 {signal_type} SIGNAL on {asset.symbol}! (Entry delayed to {datetime.fromtimestamp(entry_candle_time).strftime('%H:%M:%S')}){Colors.RESET}")
    
    TRADE_IN_PROGRESS = True
    print(f"{Colors.YELLOW}🔒 Trade lock engaged. No new signals will be sent until this trade completes.{Colors.RESET}")
    
    asyncio.create_task(process_signal_delivery_and_verification(asset, closed_candles, is_buy_sig, is_sell_sig, entry_candle_time))


async def realtime_stream(asset: Asset):
    try:
        await CLIENT.start_candles_stream(asset.symbol, PERIOD_SECONDS)
        await asyncio.sleep(0.3)
        asset.streaming = True
    except: return

    while CONNECTION_ALIVE:
        try:
            candle_data = None
            if hasattr(CLIENT, 'api') and CLIENT.api and hasattr(CLIENT.api, 'realtime_candles'):
                candle_data = CLIENT.api.realtime_candles.get(asset.symbol)
            
            if not candle_data and hasattr(CLIENT, 'get_realtime_candle'):
                try: candle_data = await asyncio.wait_for(CLIENT.get_realtime_candle(asset.symbol), timeout=1.0)
                except asyncio.TimeoutError: pass

            if candle_data:
                if isinstance(candle_data, list) and len(candle_data) >= 3:
                    ts, price = int(candle_data[1]), float(candle_data[2])
                elif isinstance(candle_data, dict):
                    ts = int(candle_data.get("time", candle_data.get("timestamp", time.time())))
                    price = float(candle_data.get("price", candle_data.get("close", 0)))
                else:
                    await asyncio.sleep(STREAM_POLL_INTERVAL); continue

                if price > 0 and ts > 0:
                    aligned_time = (ts // PERIOD_SECONDS) * PERIOD_SECONDS

                    if asset.candles and aligned_time < asset.candles[-1]['time']:
                        if aligned_time == asset.candles[-1]['time']:
                            asset.candles[-1]['high'] = max(asset.candles[-1]['high'], price)
                            asset.candles[-1]['low'] = min(asset.candles[-1]['low'], price)
                            asset.candles[-1]['close'] = price
                            asset.candles[-1]['volume'] += 1
                        await asyncio.sleep(STREAM_POLL_INTERVAL); continue

                    if asset.candles:
                        time_diff = aligned_time - asset.candles[-1]['time']
                        if time_diff > PERIOD_SECONDS:
                            missing_count = (time_diff // PERIOD_SECONDS) - 1
                            if missing_count > 1:
                                print(f"{Colors.YELLOW}⚠️ {asset.symbol}: Gap detected — {missing_count} candle(s) missing. Fetching...{Colors.RESET}")
                            fetched = await fetch_and_fill_missing_candles(asset, missing_count)
                            asset.missing_candles_filled += fetched
                            if fetched >= missing_count and missing_count > 1:
                                print(f"{Colors.GREEN}✓ {asset.symbol}: Gap fully closed ({fetched}/{missing_count}).{Colors.RESET}")
                            elif missing_count > 1:
                                print(f"{Colors.RED}✗ {asset.symbol}: Gap partially closed ({fetched}/{missing_count}).{Colors.RESET}")

                    new_candle_started = False
                    if asset.candles and asset.candles[-1]['time'] == aligned_time:
                        asset.candles[-1]['high'] = max(asset.candles[-1]['high'], price)
                        asset.candles[-1]['low'] = min(asset.candles[-1]['low'], price)
                        asset.candles[-1]['close'] = price
                        asset.candles[-1]['volume'] += 1
                    else:
                        new_candle = {'time': aligned_time, 'open': price, 'high': price, 'low': price, 'close': price, 'volume': 1}
                        add_candle_to_asset(asset, new_candle)
                        new_candle_started = True
                        asyncio.create_task(db_manager.upsert_candle(asset.symbol, new_candle))

                    asset.price = price
                    asset.updates += 1
                    asset.total_ticks += 1
                    asset.last_update_time = time.time()

                    await _maybe_check_and_fire_signal(asset)

                    await connection_manager.broadcast_tick(asset.symbol, {"symbol": asset.symbol, "price": price, "time": ts, "candle_time": aligned_time, "digits": asset.digits()})
                    current_candle = asset.candles[-1]
                    await connection_manager.broadcast_candle(asset.symbol, {"type": "update", "symbol": asset.symbol, "candle": current_candle, "new_candle": new_candle_started, "price": price})

                    now = time.time()
                    if now - asset.last_db_write >= DB_WRITE_INTERVAL:
                        asset.last_db_write = now
                        asyncio.create_task(db_manager.upsert_candle(asset.symbol, current_candle))

            await asyncio.sleep(STREAM_POLL_INTERVAL)
        except: await asyncio.sleep(1)


# ============================================
# ✅ فاحص سلامة الشموع
# ============================================
async def candle_integrity_checker():
    while True:
        await asyncio.sleep(10)
        for asset in ALL_STREAMING_ASSETS:
            if not asset.candles or len(asset.candles) < 2: continue
            recent_candles = asset.candles[-100:]
            gaps_found = 0
            for i in range(1, len(recent_candles)):
                if recent_candles[i]['time'] - recent_candles[i-1]['time'] > PERIOD_SECONDS:
                    gaps_found += 1
            if gaps_found > 0:
                asyncio.create_task(_fill_gap_background(asset, gaps_found))


async def db_background_writer():
    while True:
        try:
            for asset in ALL_STREAMING_ASSETS:
                if not asset.candles: continue
                now = time.time()
                if now - asset.last_db_write < DB_WRITE_INTERVAL: continue
                await db_manager.upsert_candle(asset.symbol, asset.candles[-1])
                await db_manager.commit()
                asset.last_db_write = now
        except: pass
        await asyncio.sleep(0.5)


# ============================================
# 🌐 FastAPI App
# ============================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    if db_manager.db is None:
        await db_manager.connect()
        for symbol in ASSET_DISPLAY_MAP: await db_manager.init_table(symbol)
    yield
    await db_manager.close()

app = FastAPI(title="QXChart Server", version="6.6.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/")
async def root(): return {"server": "QXChart Server", "version": "6.6.0", "status": "running"}

@app.get("/api/assets")
async def get_assets():
    return {"assets": [{"symbol": s, "streaming": ASSET_BY_SYMBOL.get(s).streaming if ASSET_BY_SYMBOL.get(s) else False, "price": ASSET_BY_SYMBOL.get(s).price if ASSET_BY_SYMBOL.get(s) else 0} for s in ASSET_DISPLAY_MAP]}

@app.websocket("/ws/ticks/{symbol}")
async def ws_ticks(websocket: WebSocket, symbol: str):
    if symbol not in ASSET_DISPLAY_MAP:
        await websocket.accept(); await websocket.send_json({"error": "Asset not found"}); await websocket.close(code=1008); return
    await connection_manager.accept_tick(symbol, websocket)
    try:
        asset = ASSET_BY_SYMBOL.get(symbol)
        if asset: await websocket.send_json({"symbol": symbol, "price": asset.price, "time": int(time.time()), "initial": True})
        while True:
            try: await asyncio.wait_for(websocket.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                try: await websocket.send_json({"type": "ping", "time": int(time.time())})
                except: break
    except: pass
    finally: await connection_manager.disconnect_tick(symbol, websocket)

@app.websocket("/ws/candles/{symbol}")
async def ws_candles(websocket: WebSocket, symbol: str):
    if symbol not in ASSET_DISPLAY_MAP:
        await websocket.accept(); await websocket.send_json({"error": "Asset not found"}); await websocket.close(code=1008); return
    await connection_manager.accept_candle(symbol, websocket)
    try:
        candles = await db_manager.get_candles(symbol, INITIAL_CANDLES)
        await websocket.send_json({"type": "snapshot", "symbol": symbol, "candles": candles, "count": len(candles)})
        while True:
            try: await asyncio.wait_for(websocket.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                try: await websocket.send_json({"type": "ping", "time": int(time.time())})
                except: break
    except: pass
    finally: await connection_manager.disconnect_candle(symbol, websocket)

def run_fastapi_server():
    config = uvicorn.Config(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning", access_log=False, ws_max_size=1024*1024, ws_ping_interval=20, ws_ping_timeout=20)
    server = uvicorn.Server(config)
    asyncio.set_event_loop(ASYNC_LOOP)
    try: asyncio.run_coroutine_threadsafe(server.serve(), ASYNC_LOOP).result()
    except Exception as e: print(f"{Colors.RED}❌ FastAPI Server Error: {e}{Colors.RESET}")


# ============================================
# 🎬 Main Execution
# ============================================
if __name__ == "__main__":
    print(f"\n{Colors.CYAN}{Colors.BOLD}{'═'*60}{Colors.RESET}")
    print(f"{Colors.BOLD}   QXChart Server - Pro Merged Edition v6.6{Colors.RESET}")
    print(f"{Colors.BOLD}   Exact 40 Candles + RSI TMA + Live Countdown + Royal Reports{Colors.RESET}")
    print(f"{Colors.CYAN}{'═'*60}{Colors.RESET}\n")

    print(f"{Colors.GREEN}{'═'*60}{Colors.RESET}")
    print(f"{Colors.BOLD}  📲 TELEGRAM BOT SETUP{Colors.RESET}")
    print(f"{Colors.GREEN}{'═'*60}{Colors.RESET}")
    TELEGRAM_BOT_TOKEN = input(f"  {Colors.YELLOW}🤖 Enter Bot Token:{Colors.RESET} ").strip()
    TELEGRAM_CHAT_ID = input(f"  {Colors.YELLOW}🆔 Enter Chat ID:{Colors.RESET} ").strip()

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        telegram_connected = False
        for attempt in range(1, 4):
            try:
                print(f"  {Colors.CYAN}Attempt {attempt}/3 to connect Telegram...{Colors.RESET}", end=" ")
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                payload = {"chat_id": TELEGRAM_CHAT_ID, "text": "✅ QXChart Server v6.6 Connected!"}
                response = requests.post(url, data=payload, timeout=30)
                if response.status_code == 200:
                    print(f"{Colors.GREEN}✅ Telegram connected successfully!{Colors.RESET}")
                    telegram_connected = True; break
                else:
                    print(f"{Colors.RED} Failed.{Colors.RESET}"); break
            except:
                print(f"{Colors.YELLOW}⚠️ Timed Out.{Colors.RESET}")
                time.sleep(3)

    async_thread = threading.Thread(target=start_async_engine, daemon=True, name="AsyncEngine")
    async_thread.start()
    time.sleep(0.5)

    print(f"\n{Colors.CYAN}🔍 Checking session state...{Colors.RESET}")
    should_force = session_manager.should_force_fresh()
    saved_email = session_manager.get_saved_email()
    success = False

    if saved_email and not should_force:
        print(f"  {Colors.GREEN}✓ Found saved email: {saved_email}{Colors.RESET}")
        PASSWORD_INPUT = input(f"  {Colors.YELLOW}🔑 Password:{Colors.RESET} ").strip()
        fut = asyncio.run_coroutine_threadsafe(connect_quotex(saved_email, PASSWORD_INPUT, force_fresh=False, max_attempts=3), ASYNC_LOOP)
        success = fut.result(timeout=180)

    if not success:
        print(f"\n{Colors.YELLOW}⚠️ Fresh login required.{Colors.RESET}")
        EMAIL_INPUT = input(f"  {Colors.YELLOW} Email:{Colors.RESET} ").strip()
        PASSWORD_INPUT = input(f"  {Colors.YELLOW}🔑 Password:{Colors.RESET} ").strip()
        fut = asyncio.run_coroutine_threadsafe(connect_quotex(EMAIL_INPUT, PASSWORD_INPUT, force_fresh=True, max_attempts=3), ASYNC_LOOP)
        success = fut.result(timeout=300)

    if not success:
        print(f"\n{Colors.RED}❌ Failed to connect.{Colors.RESET}"); sys.exit(1)

    print(f"\n{Colors.GREEN}✅ Connected successfully!{Colors.RESET}")

    asyncio.run_coroutine_threadsafe(db_manager.connect(), ASYNC_LOOP).result(timeout=10)
    for symbol in ASSET_DISPLAY_MAP:
        asyncio.run_coroutine_threadsafe(db_manager.init_table(symbol), ASYNC_LOOP).result(timeout=5)

    print(f"\n{Colors.CYAN}💎 Loading {len(ASSET_DISPLAY_MAP)} Assets (12-Hour Fetch)...{Colors.RESET}")
    print(f"{Colors.DIM}{'─'*50}{Colors.RESET}")
    
    all_assets = []
    tasks = []
    
    async def delayed_fetch(asset_obj, delay):
        await asyncio.sleep(delay)
        async with PRINT_LOCK:
            print(f"  {asset_obj.symbol:<12} {Colors.YELLOW}Fetching...{Colors.RESET}", flush=True)
        res = await fetch_candles_with_retry(asset_obj, max_retries=2)
        async with PRINT_LOCK:
            print(f"  {asset_obj.symbol:<12} {Colors.GREEN}Done!{Colors.RESET}", flush=True)
        return res

    for idx, symbol in enumerate(ASSET_DISPLAY_MAP):
        asset = Asset(symbol)
        all_assets.append(asset); ALL_STREAMING_ASSETS.append(asset); ASSET_BY_SYMBOL[symbol] = asset
        tasks.append(delayed_fetch(asset, idx * 3))

    async def load_all_assets_concurrent(): return await asyncio.gather(*tasks, return_exceptions=True)

    fut = asyncio.run_coroutine_threadsafe(load_all_assets_concurrent(), ASYNC_LOOP)
    results = fut.result(timeout=180)
    
    print(f"{Colors.DIM}{'─'*50}{Colors.RESET}")
    for idx, (symbol, result) in enumerate(zip(ASSET_DISPLAY_MAP, results), 1):
        if isinstance(result, Exception):
            print(f"  [{idx}] {symbol}: ❌ Fetch Error"); continue
        candles_count, _ = result
        if candles_count >= MIN_CANDLES_THRESHOLD:
            print(f"  [{idx}] {symbol:<12} ✅ Loaded {candles_count} candles.")
        else: print(f"  [{idx}] {symbol:<12} ⚠️ Only {candles_count} candles loaded.")
    
    ALL_ASSETS_LOADED = True
    print(f"\n{Colors.GREEN}✅ All assets loaded.{Colors.RESET}")

    for asset in all_assets:
        asyncio.run_coroutine_threadsafe(realtime_stream(asset), ASYNC_LOOP)

    asyncio.run_coroutine_threadsafe(health_monitor(), ASYNC_LOOP)
    asyncio.run_coroutine_threadsafe(auto_reconnect(), ASYNC_LOOP)
    asyncio.run_coroutine_threadsafe(db_background_writer(), ASYNC_LOOP)
    asyncio.run_coroutine_threadsafe(candle_integrity_checker(), ASYNC_LOOP)

    server_thread = threading.Thread(target=run_fastapi_server, daemon=True, name="FastAPIServer")
    server_thread.start()

    print(f"\n{Colors.GREEN}{'═'*60}{Colors.RESET}")
    print(f"{Colors.BOLD}  ✅ System is running.{Colors.RESET}")
    print(f"  🌐 API: http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"  🛑 Press Ctrl+C to stop.")
    print(f"{Colors.GREEN}{'═'*60}{Colors.RESET}\n")

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n\n{Colors.RED}🛑 Stopped by user.{Colors.RESET}")
        print(f"{Colors.CYAN}Missing candles filled: {sum(a.missing_candles_filled for a in all_assets):,}{Colors.RESET}\n")
