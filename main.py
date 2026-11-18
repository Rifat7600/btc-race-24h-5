#!/usr/bin/env python3
"""Dip-buy BTC bot for make.fun bars-replay/v1.

Buys sharp intraday dips in small scaled tranches and sells the bounce.
Long only, no leverage, no shorts. Deterministic: no clock, no randomness,
every decision follows from the bars seen and the hello message.
Reads newline-delimited JSON on stdin, writes one reply per batch on stdout.
Standard library only.
"""

import json
import math
import sys

# ---- strategy constants (tuned on the dev window) ----
MIN_BARS = 60            # bars of history before any order
DIP_R10 = -0.0025        # 10-minute drop that marks a dip
DIP_R5_FLOOR = -0.0010   # 5-minute drop floor
DIP_VOL_MULT = 2.0       # 5-min drop must also beat this multiple of recent 5-min vol
NEED_UPTICK = 1          # 1: only enter after the last minute ticked up (bounce started)
HIGH_LOOKBACK = 120      # dip is also measured against this many bars' high close
HIGH_DROP = 0.0          # disabled (0.0): the fast-drop + vol filter alone scores better
WINDOW_BARS = 1440       # bars in a window (dev and race alike, per the rules)
NO_ENTRY_LAST = 60       # do not open a new position this close to the window end
TRANCHE_FRAC = 0.5       # each tranche is this share of equity
MAX_TRANCHES = 2         # at most two tranches per dip
ADD_DROP = -0.0025       # second tranche when price falls this far below avg entry
TAKE_PROFIT = 0.012      # sell all when price is this far above avg entry
STOP_LOSS = -0.0035      # sell all when price is this far below avg entry
TRAIL_ARM = 0.0040       # arm the trailing exit after this peak gain
TRAIL_GIVEBACK = 0.0010  # sell when price gives back this much from the peak
MAX_HOLD = 120           # bars; sell at market after this long in a position
COOLDOWN = 10            # bars to wait after any exit before a new dip entry
MAX_SLIP_BPS = 200       # max slippage allowed on any order, vs latest close


class SymbolState:
    def __init__(self):
        self.closes = []        # close prices, integer price units
        self.tranches = 0       # buy fills since the position opened
        self.entry_bar = -1     # bar index where the position opened
        self.peak = 0           # highest close since the position opened
        self.cooldown_until = -1


class Bot:
    def __init__(self):
        self.symbols = {}       # symbol -> {price_scale, qty_scale}
        self.quote_scale = 6
        self.capital_units = 0
        self.max_open_positions = 1
        self.max_orders_per_bar = 16
        self.max_weight_bps = 10000
        self.fee_bps = 0
        self.slippage_bps = 0
        self.min_notional = 0
        self.warmup_bars = 0
        self.state = {}         # symbol -> SymbolState
        self.bar_index = -1     # batches seen (0-based count of bars processed)

    def on_hello(self, msg):
        for sym in msg.get("symbols", []):
            self.symbols[sym["symbol"]] = {
                "price_scale": sym["price_scale"],
                "qty_scale": sym["qty_scale"],
            }
            self.state[sym["symbol"]] = SymbolState()
        cap = msg.get("capital", {})
        self.quote_scale = cap.get("quote_scale", 6)
        self.capital_units = int(cap.get("quote_units", 0))
        caps = msg.get("caps", {})
        self.max_open_positions = caps.get("max_open_positions", 1)
        self.max_orders_per_bar = caps.get("max_orders_per_bar", 16)
        self.max_weight_bps = caps.get("max_weight_bps", 10000)
        fees = msg.get("fees", {})
        self.fee_bps = fees.get("fee_bps", 0)
        self.slippage_bps = fees.get("slippage_bps", 0)
        self.min_notional = int(fees.get("min_notional_quote_units", 0))
        self.warmup_bars = msg.get("warmup_bars", 0) or 0

    def on_batch(self, msg):
        seq = msg["seq"]
        account = msg.get("account", {})
        cash = int(account.get("cash_quote_units", 0))
        equity = int(account.get("equity_quote_units", cash))
        positions = {}
        for pos in account.get("positions", []):
            positions[pos["symbol"]] = {
                "qty": int(pos["qty"]),
                "cost": int(pos["cost_quote_units"]),
            }
        # record fills: a buy fill adds a tranche, a flat position resets
        for fill in msg.get("fills", []):
            if fill.get("status") != "filled":
                continue
            sym = fill["symbol"]
            st = self.state.get(sym)
            if st is None:
                continue
            if fill["side"] == "buy":
                st.tranches += 1
        orders = []
        cash_left = cash
        for bar in msg.get("bars", []):
            sym = bar["symbol"]
            if sym not in self.state:
                continue
            st = self.state[sym]
            close = int(bar["c"])
            st.closes.append(close)
            self.bar_index += 1
            pos = positions.get(sym, {"qty": 0, "cost": 0})
            qty = pos["qty"]
            if qty <= 0:
                st.tranches = 0
                st.entry_bar = -1
                st.peak = 0
            else:
                if st.entry_bar < 0:
                    st.entry_bar = self.bar_index
                    st.peak = close
                if close > st.peak:
                    st.peak = close
            order = self.decide(sym, st, close, qty, pos, cash_left, equity)
            if order is not None:
                orders.append(order)
                if order["side"] == "buy":
                    cash_left -= int(order["notional_quote_units"])
                if len(orders) >= self.max_orders_per_bar:
                    break
        return {"type": "reply", "seq": seq, "orders": orders}

    def decide(self, sym, st, close, qty, pos, cash_left, equity):
        if len(st.closes) < MIN_BARS:
            return None
        if self.bar_index < self.warmup_bars:
            return None
        n = len(st.closes)
        # average entry price in integer price units (cost / qty)
        avg_entry = 0
        if qty > 0:
            avg_entry = pos["cost"] * 10 ** 7 // qty
        if qty > 0:
            move = close / avg_entry - 1.0 if avg_entry > 0 else 0.0
            held = self.bar_index - st.entry_bar
            peak_gain = st.peak / avg_entry - 1.0 if avg_entry > 0 else 0.0
            giveback = st.peak / close - 1.0 if close > 0 else 0.0
            if move >= TAKE_PROFIT:
                return self.sell(sym, qty)
            if move <= STOP_LOSS:
                return self.sell(sym, qty)
            if peak_gain >= TRAIL_ARM and giveback >= TRAIL_GIVEBACK:
                return self.sell(sym, qty)
            if held >= MAX_HOLD:
                return self.sell(sym, qty)
            # second tranche: price fell further below the average entry
            if st.tranches == 1 and move <= ADD_DROP:
                pos_value = qty * close // 10 ** 7
                return self.buy(sym, cash_left, equity, pos_value)
            return None
        # flat: look for a dip, once the cooldown has passed
        if self.bar_index < st.cooldown_until:
            return None
        # no new position close to the window end: no time left for a bounce
        if self.bar_index >= WINDOW_BARS - NO_ENTRY_LAST:
            return None
        if not self.is_dip(st):
            return None
        return self.buy(sym, cash_left, equity, 0)

    def is_dip(self, st):
        closes = st.closes
        n = len(closes)
        if n < 61:
            return False
        r5 = closes[-1] / closes[-6] - 1.0
        r10 = closes[-1] / closes[-11] - 1.0
        if r10 > DIP_R10:
            return False
        # recent volatility of 5-minute returns
        rets5 = []
        for i in range(n - 60, n):
            if closes[i - 5] > 0:
                rets5.append(closes[i] / closes[i - 5] - 1.0)
        if len(rets5) < 20:
            return False
        mean5 = sum(rets5) / len(rets5)
        var5 = sum((r - mean5) ** 2 for r in rets5) / len(rets5)
        sigma5 = math.sqrt(var5)
        thresh = min(DIP_R5_FLOOR, -DIP_VOL_MULT * sigma5)
        if r5 > thresh:
            return False
        # wait for the bounce to start: the last minute ticked up
        if NEED_UPTICK and closes[-1] <= closes[-2]:
            return False
        # deep enough below the recent high: a real dip, not a ripple
        if n >= HIGH_LOOKBACK:
            high = max(closes[-HIGH_LOOKBACK:])
            if closes[-1] > high * (1.0 + HIGH_DROP):
                return False
        return True

    def buy(self, sym, cash_left, equity, pos_value):
        notional = int(equity * TRANCHE_FRAC)
        if notional < self.min_notional:
            return None
        if notional > cash_left:
            notional = cash_left
        if notional < self.min_notional:
            return None
        # respect the max position weight: position after the buy <= cap
        cap_notional = int(equity * self.max_weight_bps / 10000) - pos_value
        if notional > cap_notional:
            notional = cap_notional
        if notional < self.min_notional:
            return None
        return {
            "side": "buy",
            "client_id": "b%d" % self.bar_index,
            "symbol": sym,
            "notional_quote_units": str(notional),
            "max_slippage_bps": MAX_SLIP_BPS,
        }

    def sell(self, sym, qty):
        st = self.state[sym]
        st.cooldown_until = self.bar_index + COOLDOWN
        return {
            "side": "sell",
            "client_id": "s%d" % self.bar_index,
            "symbol": sym,
            "qty": str(qty),
            "max_slippage_bps": MAX_SLIP_BPS,
        }


def main():
    bot = Bot()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get("type")
        if kind == "hello":
            bot.on_hello(msg)
        elif kind == "batch":
            reply = bot.on_batch(msg)
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            break


if __name__ == "__main__":
    main()
