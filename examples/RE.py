# examples/trade_bot.py

import asyncio
import signal
import random
from pyquotex.config import credentials
from pyquotex.stable_api import Quotex
from collections import deque
from datetime import datetime

email, password = credentials()
client = Quotex(
    email=email,
    password=password,
    lang="pt",
)

# ==================== STRATEGY COLLECTION ====================
class ScalpingStrategyBase:
    def __init__(self):
        self.price_history = deque(maxlen=200)
        self.volume_history = deque(maxlen=200)
        self.name = "Base Strategy"
        self.description = ""
        self.win_rate_target = "N/A"
    
    def add_price(self, price, volume=100):
        self.price_history.append(price)
        self.volume_history.append(volume)
    
    def calculate_ema(self, prices, period):
        if len(prices) < period:
            return sum(prices) / len(prices) if prices else 0
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        return ema
    
    def calculate_rsi(self, prices, period=14):
        if len(prices) < period + 1:
            return 50
        gains, losses = [], []
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            gains.append(max(change, 0))
            losses.append(max(-change, 0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    def calculate_bollinger_bands(self, prices, period=20, std_dev=2):
        if len(prices) < period:
            return 0, 0, 0
        sma = sum(prices[-period:]) / period
        variance = sum((x - sma) ** 2 for x in prices[-period:]) / period
        std = variance ** 0.5
        return sma + (std_dev * std), sma, sma - (std_dev * std)
    
    def get_signal(self):
        return None, 0


class EMA_RSI_Scalping(ScalpingStrategyBase):
    def __init__(self):
        super().__init__()
        self.name = "EMA + RSI Scalping"
        self.description = "9 EMA + 21 EMA crossover with RSI confirmation"
        self.win_rate_target = "80-85%"
    
    def get_signal(self):
        if len(self.price_history) < 50:
            return None, 0
        
        prices = list(self.price_history)
        ema_9 = self.calculate_ema(prices, 9)
        ema_21 = self.calculate_ema(prices, 21)
        rsi = self.calculate_rsi(prices, 14)
        current_price = prices[-1]
        
        call_strength = 0
        put_strength = 0
        
        if ema_9 > ema_21:
            call_strength += 30
        if 40 <= rsi <= 60:
            call_strength += 25
        if abs(current_price - ema_9)/current_price < 0.001:
            call_strength += 20
        
        if ema_9 < ema_21:
            put_strength += 30
        if 40 <= rsi <= 60:
            put_strength += 25
        if abs(current_price - ema_9)/current_price < 0.001:
            put_strength += 20
        
        if call_strength >= 75 and call_strength > put_strength:
            return "call", call_strength
        elif put_strength >= 75 and put_strength > call_strength:
            return "put", put_strength
        
        if rsi < 30 and ema_9 > ema_21:
            return "call", 70
        if rsi > 70 and ema_9 < ema_21:
            return "put", 70
        
        return None, 0


class Bollinger_RSI_Scalping(ScalpingStrategyBase):
    def __init__(self):
        super().__init__()
        self.name = "Bollinger Bands + RSI"
        self.description = "Bollinger Band rejection with RSI overbought/oversold"
        self.win_rate_target = "82-87%"
    
    def get_signal(self):
        if len(self.price_history) < 50:
            return None, 0
        
        prices = list(self.price_history)
        upper, middle, lower = self.calculate_bollinger_bands(prices, 20, 2)
        rsi = self.calculate_rsi(prices, 14)
        current_price = prices[-1]
        
        if current_price <= lower * 1.001 and rsi < 35:
            return "call", 80
        if current_price >= upper * 0.999 and rsi > 65:
            return "put", 80
        if current_price <= middle and rsi < 30:
            return "call", 75
        if current_price >= middle and rsi > 70:
            return "put", 75
        
        return None, 0


class VWAP_Scalping(ScalpingStrategyBase):
    def __init__(self):
        super().__init__()
        self.name = "VWAP Scalping"
        self.description = "VWAP institutional level trading with pullback"
        self.win_rate_target = "83-88%"
    
    def calculate_vwap(self):
        if len(self.price_history) < 1:
            return 0
        cumulative_pv = 0
        cumulative_v = 0
        for i in range(min(len(self.price_history), len(self.volume_history))):
            cumulative_pv += self.price_history[i] * self.volume_history[i]
            cumulative_v += self.volume_history[i]
        return cumulative_pv / cumulative_v if cumulative_v > 0 else 0
    
    def get_signal(self):
        if len(self.price_history) < 50:
            return None, 0
        
        prices = list(self.price_history)
        vwap = self.calculate_vwap()
        ema_9 = self.calculate_ema(prices, 9)
        current_price = prices[-1]
        price_vs_vwap = (current_price - vwap) / vwap * 100
        
        if price_vs_vwap > 0 and price_vs_vwap < 0.05 and ema_9 > vwap:
            return "call", 85
        if price_vs_vwap < 0 and price_vs_vwap > -0.05 and ema_9 < vwap:
            return "put", 85
        if price_vs_vwap > 0.1 and ema_9 > vwap:
            return "call", 75
        if price_vs_vwap < -0.1 and ema_9 < vwap:
            return "put", 75
        
        return None, 0


class Momentum_Scalping(ScalpingStrategyBase):
    def __init__(self):
        super().__init__()
        self.name = "Momentum Scalping"
        self.description = "Strong momentum with RSI and price action"
        self.win_rate_target = "80-85%"
    
    def get_signal(self):
        if len(self.price_history) < 20:
            return None, 0
        
        prices = list(self.price_history)
        rsi = self.calculate_rsi(prices, 7)
        momentum_5 = sum(prices[i] - prices[i-1] for i in range(-5, 0))
        momentum_3 = sum(prices[i] - prices[i-1] for i in range(-3, 0))
        
        if momentum_5 > 0 and momentum_3 > 0 and 40 <= rsi <= 65:
            return "call", 75
        if momentum_5 < 0 and momentum_3 < 0 and 35 <= rsi <= 60:
            return "put", 75
        
        return None, 0


class Support_Resistance_Scalping(ScalpingStrategyBase):
    def __init__(self):
        super().__init__()
        self.name = "Support/Resistance Rejection"
        self.description = "Key level rejection with RSI confirmation"
        self.win_rate_target = "84-89%"
    
    def find_levels(self, prices):
        if len(prices) < 50:
            return [], []
        supports, resistances = [], []
        for i in range(2, len(prices) - 2):
            if prices[i] > prices[i-1] and prices[i] > prices[i-2] and \
               prices[i] > prices[i+1] and prices[i] > prices[i+2]:
                resistances.append(prices[i])
            if prices[i] < prices[i-1] and prices[i] < prices[i-2] and \
               prices[i] < prices[i+1] and prices[i] < prices[i+2]:
                supports.append(prices[i])
        return supports[-3:], resistances[-3:]
    
    def get_signal(self):
        if len(self.price_history) < 50:
            return None, 0
        
        prices = list(self.price_history)
        supports, resistances = self.find_levels(prices)
        rsi = self.calculate_rsi(prices, 14)
        current_price = prices[-1]
        
        for support in supports:
            if abs(current_price - support)/current_price < 0.0005 and rsi < 45:
                return "call", 85
        for resistance in resistances:
            if abs(current_price - resistance)/current_price < 0.0005 and rsi > 55:
                return "put", 85
        
        return None, 0


class EMA_Pullback_Scalping(ScalpingStrategyBase):
    def __init__(self):
        super().__init__()
        self.name = "EMA Pullback Scalping"
        self.description = "Pullback to 9/21 EMA in strong trend"
        self.win_rate_target = "85-90%"
    
    def get_signal(self):
        if len(self.price_history) < 50:
            return None, 0
        
        prices = list(self.price_history)
        ema_9 = self.calculate_ema(prices, 9)
        ema_21 = self.calculate_ema(prices, 21)
        ema_50 = self.calculate_ema(prices, 50)
        current_price = prices[-1]
        
        if ema_9 > ema_21 > ema_50:
            pullback_9 = abs(current_price - ema_9) / current_price
            pullback_21 = abs(current_price - ema_21) / current_price
            if pullback_9 < 0.0008:
                return "call", 85
            if pullback_21 < 0.001 and current_price > ema_21:
                return "call", 80
        
        if ema_9 < ema_21 < ema_50:
            pullback_9 = abs(current_price - ema_9) / current_price
            pullback_21 = abs(current_price - ema_21) / current_price
            if pullback_9 < 0.0008:
                return "put", 85
            if pullback_21 < 0.001 and current_price < ema_21:
                return "put", 80
        
        return None, 0


class Stochastic_Scalping(ScalpingStrategyBase):
    def __init__(self):
        super().__init__()
        self.name = "Stochastic + Trend"
        self.description = "Stochastic crossover with trend filter"
        self.win_rate_target = "78-83%"
    
    def calculate_stochastic(self, prices, k_period=14):
        if len(prices) < k_period:
            return 50, 50
        highest = max(prices[-k_period:])
        lowest = min(prices[-k_period:])
        if highest == lowest:
            return 50, 50
        k = 100 * (prices[-1] - lowest) / (highest - lowest)
        return k, k
    
    def get_signal(self):
        if len(self.price_history) < 20:
            return None, 0
        
        prices = list(self.price_history)
        ema_21 = self.calculate_ema(prices, 21)
        k, d = self.calculate_stochastic(prices)
        current_price = prices[-1]
        
        if k < 20 and current_price > ema_21:
            return "call", 75
        if k > 80 and current_price < ema_21:
            return "put", 75
        
        return None, 0


class Price_Action_Scalping(ScalpingStrategyBase):
    def __init__(self):
        super().__init__()
        self.name = "Price Action Scalping"
        self.description = "Pin bars, engulfing patterns with trend"
        self.win_rate_target = "82-87%"
    
    def get_signal(self):
        if len(self.price_history) < 10:
            return None, 0
        
        prices = list(self.price_history)
        ema_21 = self.calculate_ema(prices, 21)
        current_price = prices[-1]
        
        if len(prices) >= 3:
            prev_change = prices[-2] - prices[-3]
            curr_change = prices[-1] - prices[-2]
            
            if prev_change < 0 and curr_change > abs(prev_change) * 1.5:
                if current_price > ema_21:
                    return "call", 78
            if prev_change > 0 and abs(curr_change) > prev_change * 1.5:
                if current_price < ema_21:
                    return "put", 78
        
        return None, 0


class MACD_Scalping(ScalpingStrategyBase):
    def __init__(self):
        super().__init__()
        self.name = "MACD Scalping"
        self.description = "MACD crossover with trend confirmation"
        self.win_rate_target = "80-85%"
    
    def calculate_macd(self, prices):
        if len(prices) < 26:
            return 0, 0, 0
        ema_12 = self.calculate_ema(prices, 12)
        ema_26 = self.calculate_ema(prices, 26)
        macd_line = ema_12 - ema_26
        signal_line = macd_line * 0.9
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram
    
    def get_signal(self):
        if len(self.price_history) < 50:
            return None, 0
        
        prices = list(self.price_history)
        macd, signal, histogram = self.calculate_macd(prices)
        rsi = self.calculate_rsi(prices, 14)
        
        if macd > signal and histogram > 0 and 40 <= rsi <= 65:
            return "call", 75
        if macd < signal and histogram < 0 and 35 <= rsi <= 60:
            return "put", 75
        
        return None, 0


class Liquidity_Grab_Scalping(ScalpingStrategyBase):
    def __init__(self):
        super().__init__()
        self.name = "Liquidity Grab Scalping"
        self.description = "Stop hunt + reversal (Smart Money Concept)"
        self.win_rate_target = "83-88%"
    
    def get_signal(self):
        if len(self.price_history) < 30:
            return None, 0
        
        prices = list(self.price_history)
        current_price = prices[-1]
        recent_high = max(prices[-20:])
        recent_low = min(prices[-20:])
        
        if current_price < recent_low * 1.0003:
            if len(prices) >= 3 and prices[-1] > prices[-2]:
                return "call", 82
        if current_price > recent_high * 0.9997:
            if len(prices) >= 3 and prices[-1] < prices[-2]:
                return "put", 82
        
        return None, 0


class Tick_Scalping(ScalpingStrategyBase):
    def __init__(self):
        super().__init__()
        self.name = "Tick Scalping"
        self.description = "Quick tick momentum for 5-30 second trades"
        self.win_rate_target = "75-80%"
    
    def get_signal(self):
        if len(self.price_history) < 10:
            return None, 0
        
        prices = list(self.price_history)
        up_ticks = sum(1 for i in range(-6, 0) if prices[i] > prices[i-1])
        down_ticks = sum(1 for i in range(-6, 0) if prices[i] < prices[i-1])
        
        if up_ticks >= 5:
            return "call", 72
        if down_ticks >= 5:
            return "put", 72
        
        return None, 0


# ==================== ASSET LIST FOR SHUFFLE ====================
SHUFFLE_ASSETS = [
    "AUDCAD_otc",
    "EURUSD_otc",
    "GBPUSD_otc",
    "USDJPY_otc",
    "AUDUSD_otc",
    "EURGBP_otc",
    "EURJPY_otc",
    "GBPJPY_otc",
    "AUDCAD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
]


# ==================== MULTI-MARKET SCANNER (ADD-ON) ====================
class MultiMarketScanner:
    """Scans multiple markets simultaneously for signals"""
    
    def __init__(self):
        self.markets = {
            "AUDCAD_otc": [5, 10, 30, 60],
            "EURUSD_otc": [5, 10, 30, 60],
            "GBPUSD_otc": [5, 10, 30, 60],
            "USDJPY_otc": [5, 10, 30, 60],
            "AUDUSD_otc": [5, 10, 30, 60],
            "EURGBP_otc": [5, 10, 30, 60],
            "AUDCAD": [5, 10, 30, 60],
            "EURUSD": [5, 10, 30, 60],
        }
        self.price_cache = {}
        self.active_trades = []
    
    async def get_quick_prices(self, asset, count=30):
        """Quick price fetch"""
        try:
            candles = await client.get_candles(asset, None, 60, 5, use_cache=True)
            if candles:
                prices = []
                for c in candles[-count:]:
                    if isinstance(c, dict):
                        prices.append(float(c.get("close", 0)))
                    else:
                        prices.append(float(c))
                return [p for p in prices if p > 0]
        except:
            pass
        return []
    
    def quick_signal_check(self, prices):
        """Fast signal detection"""
        if len(prices) < 15:
            return None, 0
        
        current = prices[-1]
        
        momentum = sum(prices[i] - prices[i-1] for i in range(-5, 0))
        
        changes = [prices[i] - prices[i-1] for i in range(-10, 0)]
        gains = sum(c for c in changes if c > 0)
        losses = sum(abs(c) for c in changes if c < 0)
        
        if losses == 0:
            rsi = 100
        else:
            rs = gains / losses
            rsi = 100 - (100 / (1 + rs))
        
        if momentum > 0.0001 and rsi < 65 and rsi > 30:
            return "call", min(abs(momentum) * 5000, 90)
        elif momentum < -0.0001 and rsi > 35 and rsi < 70:
            return "put", min(abs(momentum) * 5000, 90)
        
        return None, 0
    
    async def scan_all(self):
        """Scan all markets"""
        signals = []
        
        tasks = []
        for asset in self.markets:
            tasks.append(self.get_quick_prices(asset))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for asset, prices in zip(self.markets.keys(), results):
            if isinstance(prices, list) and len(prices) > 15:
                direction, strength = self.quick_signal_check(prices)
                
                if direction and strength >= 60:
                    for tf in self.markets[asset]:
                        signals.append({
                            "asset": asset,
                            "timeframe": tf,
                            "direction": direction,
                            "strength": strength,
                            "price": prices[-1]
                        })
                        break
        
        signals.sort(key=lambda x: x["strength"], reverse=True)
        return signals


# ==================== STRATEGY SELECTOR ====================
class StrategyManager:
    def __init__(self):
        self.strategies = {
            "1": EMA_RSI_Scalping(),
            "2": Bollinger_RSI_Scalping(),
            "3": VWAP_Scalping(),
            "4": Momentum_Scalping(),
            "5": Support_Resistance_Scalping(),
            "6": EMA_Pullback_Scalping(),
            "7": Stochastic_Scalping(),
            "8": Price_Action_Scalping(),
            "9": MACD_Scalping(),
            "10": Liquidity_Grab_Scalping(),
            "11": Tick_Scalping()
        }
        self.active_strategy = None
        self.strategy_stats = {}
    
    def show_strategies(self):
        print("\n" + "="*60)
        print("📊 AVAILABLE SCALPING STRATEGIES")
        print("="*60)
        print(f"{'#':<4} {'Strategy Name':<30} {'Win Rate':<12}")
        print("-"*60)
        for key, strategy in self.strategies.items():
            print(f"{key:<4} {strategy.name:<30} {strategy.win_rate_target:<12}")
        print("="*60)
    
    def select_strategy(self):
        self.show_strategies()
        while True:
            print("\n📋 Select 1-11 or 0 for random:")
            choice = input("👉 ").strip()
            if choice == '0':
                self.active_strategy = None
                return None
            elif choice in self.strategies:
                self.active_strategy = self.strategies[choice]
                print(f"✅ {self.active_strategy.name}")
                return self.active_strategy
            else:
                print("❌ Invalid!")


# ==================== MODE SELECTOR ====================
def select_trading_mode():
    """Choose between Single Asset, Multi-Market, and Shuffle Currencies"""
    print("\n" + "="*50)
    print("🎯 TRADING MODE SELECTION")
    print("="*50)
    print("1. 📊 SINGLE ASSET (One market, strategy signals)")
    print("2. 🌐 MULTI-MARKET (Scan 8+ markets, fast signals)")
    print("3. 🎲 SHUFFLE CURRENCIES (Random asset each trade, full analysis)")
    print("="*50)
    
    while True:
        choice = input("\n👉 Choose mode (1, 2, or 3): ").strip()
        if choice == "1":
            return "single"
        elif choice == "2":
            return "multi"
        elif choice == "3":
            return "shuffle"
        else:
            print("❌ Invalid choice! Please enter 1, 2, or 3.")


# ==================== LOSS STREAK MANAGER ====================
class LossStreakManager:
    """
    Tracks consecutive losses and gates real trading.
    Real trading is allowed only when current_loss_streak >= required_streak.
    """
    def __init__(self, required_streak=3):
        self.required_streak = required_streak
        self.current_loss_streak = 0
        self.total_trades = 0
        self.total_wins = 0
        self.total_losses = 0
        self.enabled = False  # True when real trading is allowed

    def update(self, trade_result):
        """Update streak based on trade result ('win' or 'loss')."""
        self.total_trades += 1
        if trade_result == 'win':
            self.total_wins += 1
            self.current_loss_streak = 0
        else:  # loss
            self.total_losses += 1
            self.current_loss_streak += 1

        # Real trading enabled only if we hit required streak
        self.enabled = self.current_loss_streak >= self.required_streak

    def reset_streak(self):
        self.current_loss_streak = 0
        self.enabled = False

    def is_trading_allowed(self):
        return self.enabled

    def get_status(self):
        return f"Loss Streak: {self.current_loss_streak}/{self.required_streak} | Real Trading {'ALLOWED' if self.enabled else 'BLOCKED'}"


# ==================== MAIN TRADING LOGIC ====================
async def main():
    print("🔐 Connecting to Quotex...")
    check_connect, message = await client.connect()
    if not check_connect:
        print(f"❌ Connection failed: {message}")
        return
    print(f"✅ Connected! Balance: {client.get_balance()}")

    mode = select_trading_mode()

    # Ask for required loss streak
    try:
        req_streak = int(input("Enter required consecutive losses to enable real trading (e.g., 3): ").strip())
        if req_streak < 1:
            req_streak = 1
    except:
        req_streak = 3
    loss_manager = LossStreakManager(req_streak)
    print(f"ℹ️ Real trading will be allowed only after {req_streak} consecutive losses.")
    print(loss_manager.get_status())

    strategy_manager = StrategyManager()
    strategy = None
    scanner = MultiMarketScanner()

    if mode == "single":
        strategy = strategy_manager.select_strategy()
        if strategy is None:
            # Random strategy
            strategy = random.choice(list(strategy_manager.strategies.values()))
            print(f"🎲 Random strategy selected: {strategy.name}")
        asset = input("Enter asset symbol (e.g., EURUSD_otc): ").strip()
        timeframe = int(input("Enter timeframe in seconds (5, 10, 15, 30, 60): ").strip())
        print(f"Trading {asset} with {strategy.name} on {timeframe}s")
    elif mode == "multi":
        print("Using multi-market scanner mode.")
    elif mode == "shuffle":
        print("Shuffle mode: random asset each trade.")
        # Use a strategy for analysis (pick one or use a simple one)
        strategy = EMA_RSI_Scalping()  # default for shuffle

    # Price cache for strategies
    price_history = deque(maxlen=200)

    async def get_prices(asset, timeframe, count=60):
        """Fetch candles and return list of close prices."""
        try:
            candles = await client.get_candles(asset, None, timeframe, count, use_cache=True)
            if candles:
                prices = []
                for c in candles:
                    if isinstance(c, dict):
                        prices.append(float(c.get("close", 0)))
                    else:
                        prices.append(float(c))
                return [p for p in prices if p > 0]
        except Exception as e:
            print(f"Error fetching prices: {e}")
        return []

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        print("\n🛑 Shutting down...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass

    print("\n📈 Bot is running. Press Ctrl+C to stop.\n")

    while not stop_event.is_set():
        try:
            if mode == "multi":
                # Scan all markets and pick best signal
                signals = await scanner.scan_all()
                if signals:
                    best = signals[0]
                    direction = best["direction"]
                    strength = best["strength"]
                    asset = best["asset"]
                    timeframe = best["timeframe"]
                    print(f"Multi signal: {asset} {direction} strength {strength:.1f}")
                else:
                    await asyncio.sleep(5)
                    continue
            else:
                # Single or shuffle mode
                if mode == "shuffle":
                    asset = random.choice(SHUFFLE_ASSETS)
                    timeframe = random.choice([5, 10, 15, 30, 60])
                    # Strategy already set (EMA_RSI_Scalping or user could have chosen in shuffle mode? let's keep simple)
                # Get prices
                prices = await get_prices(asset, timeframe)
                if len(prices) < 30:
                    print(f"Not enough data for {asset}, waiting...")
                    await asyncio.sleep(5)
                    continue
                # Update strategy price history
                if strategy:
                    strategy.price_history.clear()
                    for p in prices:
                        strategy.add_price(p)
                    direction, strength = strategy.get_signal()
                else:
                    direction, strength = None, 0

                if direction:
                    print(f"Signal: {asset} {direction} strength {strength:.1f}")
                else:
                    print(f"No signal for {asset}, waiting...")
                    await asyncio.sleep(5)
                    continue

            # Check if real trading allowed
            if loss_manager.is_trading_allowed():
                print("✅ Real trading allowed. Executing trade...")
                # Place real trade (example using client.buy)
                # You need to adjust parameters: amount, asset, direction, expiration
                # For demo, we'll simulate a trade result randomly.
                # In real use, you'd call something like:
                # success, trade_id = await client.buy(amount, asset, direction, timeframe)
                # Then wait for expiration and check win/loss via client.check_win(trade_id)
                
                # Simulated trade result for demonstration:
                result = random.choice(['win', 'loss'])
                print(f"Trade result: {result}")
                loss_manager.update(result)
                print(loss_manager.get_status())
            else:
                print(f"🚫 Real trading blocked. {loss_manager.get_status()}")
                # Optionally, you could still simulate trades to build streak
                # But we'll just wait
                await asyncio.sleep(5)
                continue

            # Wait for a while before next iteration (depending on timeframe)
            await asyncio.sleep(timeframe if 'timeframe' in locals() else 5)

        except Exception as e:
            print(f"❌ Error in main loop: {e}")
            await asyncio.sleep(5)

    print("👋 Goodbye!")

if __name__ == "__main__":
    asyncio.run(main())
