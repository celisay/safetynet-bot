#!/usr/bin/env python3
"""
SafetyNet DCA Bot for Bybit (Spot) — Paper Trading via Bybit Testnet
====================================================================
Strategy: DCA "safety net" (base order + scaled safety orders, take-profit
from average entry) wrapped in strict capital-preservation rules:

  - Each deal uses only a small % of total capital (per_deal_budget_pct)
  - Hard stop-loss per deal -> max loss per deal ~= 1-2% of capital
  - Max concurrent deals cap (total exposure limit)
  - Account-level circuit breaker: stops trading after max drawdown

Runs on Bybit TESTNET by default (fake money, real order flow).
Set "testnet": false in config.json ONLY after weeks of successful paper
trading. You have been warned.
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import requests
from pybit.unified_trading import HTTP

# ---------------------------------------------------------------- paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "bot.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("safetynet")


# ---------------------------------------------------------------- helpers
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- bot
class SafetyNetBot:
    def __init__(self, config):
        self.cfg = config
        self.paper = config.get("mode", "paper") == "paper"
        if self.paper:
            # Public endpoints only — no account or API key required.
            self.session = HTTP(testnet=False)
            self.paper_fee = config.get("paper_fee_pct", 0.1) / 100.0
        else:
            self.session = HTTP(
                testnet=config.get("testnet", True),
                api_key=config["api_key"],
                api_secret=config["api_secret"],
            )
        self.state = load_json(STATE_PATH, {
            "deals": {},          # symbol -> active deal dict
            "realized_pnl": 0.0,  # cumulative realized PnL (USDT)
            "closed_deals": [],   # history
            "halted": False,
        })
        self.instrument_cache = {}
        self._validate_capital_rules()

    # ------------------------------------------------ capital math
    def _validate_capital_rules(self):
        c = self.cfg
        budget = c["total_capital_usdt"] * c["per_deal_budget_pct"] / 100.0
        # geometric sum of safety-order multipliers
        mult = 1.0
        so_mult = c["first_safety_order_multiplier"]
        for _ in range(c["max_safety_orders"]):
            mult += so_mult
            so_mult *= c["safety_order_volume_scale"]
        self.base_order_usdt = round(budget / mult, 2)
        self.deal_budget = budget

        worst_loss = budget * c["stop_loss_pct"] / 100.0
        worst_loss_pct_of_capital = worst_loss / c["total_capital_usdt"] * 100
        total_exposure_pct = c["per_deal_budget_pct"] * c["max_concurrent_deals"]

        log.info("=== CAPITAL RULES ===")
        log.info("Total capital: %.2f USDT", c["total_capital_usdt"])
        log.info("Per-deal budget: %.2f USDT (%.1f%% of capital)",
                 budget, c["per_deal_budget_pct"])
        log.info("Base order: %.2f USDT, up to %d safety orders",
                 self.base_order_usdt, c["max_safety_orders"])
        log.info("Worst-case loss per deal: %.2f USDT (%.2f%% of capital)",
                 worst_loss, worst_loss_pct_of_capital)
        log.info("Max total exposure: %.1f%% of capital", total_exposure_pct)

        if worst_loss_pct_of_capital > 2.0:
            log.error("REFUSING TO START: worst-case loss per deal is %.2f%% "
                      "of capital (limit 2%%). Lower per_deal_budget_pct or "
                      "stop_loss_pct.", worst_loss_pct_of_capital)
            sys.exit(1)
        if total_exposure_pct > 10.0:
            log.error("REFUSING TO START: max total exposure %.1f%% > 10%%. "
                      "Lower max_concurrent_deals or per_deal_budget_pct.",
                      total_exposure_pct)
            sys.exit(1)

    # ------------------------------------------------ exchange I/O
    def instrument(self, symbol):
        if symbol not in self.instrument_cache:
            r = self.session.get_instruments_info(category="spot", symbol=symbol)
            info = r["result"]["list"][0]
            self.instrument_cache[symbol] = {
                "base_precision": float(info["lotSizeFilter"]["basePrecision"]),
                "min_order_amt": float(info["lotSizeFilter"]["minOrderAmt"]),
            }
        return self.instrument_cache[symbol]

    def price(self, symbol):
        r = self.session.get_tickers(category="spot", symbol=symbol)
        return float(r["result"]["list"][0]["lastPrice"])

    def market_buy_quote(self, symbol, usdt_amount):
        """Market buy spending a USDT amount. Returns (qty, avg_price)."""
        if self.paper:
            px = self.price(symbol)
            bal = self.state.setdefault("paper_usdt",
                                        self.cfg["total_capital_usdt"])
            spend = min(usdt_amount, bal)
            qty = (spend * (1 - self.paper_fee)) / px
            self.state["paper_usdt"] = bal - spend
            save_json(STATE_PATH, self.state)
            return qty, px
        r = self.session.place_order(
            category="spot", symbol=symbol, side="Buy",
            orderType="Market", qty=str(round(usdt_amount, 2)),
            marketUnit="quoteCoin",
        )
        order_id = r["result"]["orderId"]
        return self._filled(symbol, order_id)

    def market_sell_base(self, symbol, qty):
        if self.paper:
            px = self.price(symbol)
            proceeds = qty * px * (1 - self.paper_fee)
            self.state["paper_usdt"] = self.state.get(
                "paper_usdt", self.cfg["total_capital_usdt"]) + proceeds
            save_json(STATE_PATH, self.state)
            return qty, proceeds / qty
        prec = self.instrument(symbol)["base_precision"]
        qty = self._round_step(qty, prec)
        r = self.session.place_order(
            category="spot", symbol=symbol, side="Sell",
            orderType="Market", qty=str(qty), marketUnit="baseCoin",
        )
        order_id = r["result"]["orderId"]
        return self._filled(symbol, order_id)

    def _filled(self, symbol, order_id, tries=10):
        for _ in range(tries):
            r = self.session.get_order_history(
                category="spot", symbol=symbol, orderId=order_id)
            lst = r["result"]["list"]
            if lst and lst[0]["orderStatus"] in ("Filled", "PartiallyFilledCanceled"):
                o = lst[0]
                qty = float(o["cumExecQty"])
                value = float(o["cumExecValue"])
                avg = value / qty if qty else 0.0
                return qty, avg
            time.sleep(1)
        raise RuntimeError(f"Order {order_id} not filled after {tries}s")

    @staticmethod
    def _round_step(qty, step):
        return float(int(qty / step) * step)

    # ------------------------------------------------ notifications
    def notify(self, msg):
        log.info("NOTIFY: %s", msg)
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or self.cfg.get("telegram_bot_token", "")
        chat = os.environ.get("TELEGRAM_CHAT_ID") or self.cfg.get("telegram_chat_id", "")
        if token and chat:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": msg}, timeout=10)
            except Exception as e:
                log.warning("Telegram notify failed: %s", e)

    # ------------------------------------------------ deal lifecycle
    def open_deal(self, symbol):
        px = self.price(symbol)
        qty, avg = self.market_buy_quote(symbol, self.base_order_usdt)
        deal = {
            "symbol": symbol,
            "opened_at": now_iso(),
            "qty": qty,
            "spent": qty * avg,
            "avg_entry": avg,
            "last_fill_price": avg,
            "safety_orders_used": 0,
            "next_so_deviation_pct": self.cfg["safety_order_deviation_pct"],
            "next_so_usdt": self.base_order_usdt * self.cfg["first_safety_order_multiplier"],
        }
        self.state["deals"][symbol] = deal
        save_json(STATE_PATH, self.state)
        self.notify(f"🟢 OPENED {symbol} deal: {qty:.6f} @ {avg:.4f} "
                    f"({deal['spent']:.2f} USDT)")

    def add_safety_order(self, symbol, deal):
        usdt = min(deal["next_so_usdt"],
                   self.deal_budget - deal["spent"])
        min_amt = 1.0 if self.paper else self.instrument(symbol)["min_order_amt"]
        if usdt < min_amt:
            return
        qty, avg = self.market_buy_quote(symbol, usdt)
        deal["qty"] += qty
        deal["spent"] += qty * avg
        deal["avg_entry"] = deal["spent"] / deal["qty"]
        deal["last_fill_price"] = avg
        deal["safety_orders_used"] += 1
        deal["next_so_deviation_pct"] *= self.cfg["safety_order_step_scale"]
        deal["next_so_usdt"] *= self.cfg["safety_order_volume_scale"]
        save_json(STATE_PATH, self.state)
        self.notify(f"🛟 SAFETY ORDER {deal['safety_orders_used']}/"
                    f"{self.cfg['max_safety_orders']} {symbol}: bought "
                    f"{qty:.6f} @ {avg:.4f}. New avg {deal['avg_entry']:.4f}")

    def close_deal(self, symbol, deal, reason):
        qty, avg = self.market_sell_base(symbol, deal["qty"])
        proceeds = qty * avg
        pnl = proceeds - deal["spent"]
        self.state["realized_pnl"] += pnl
        self.state["closed_deals"].append({
            "symbol": symbol, "opened_at": deal["opened_at"],
            "closed_at": now_iso(), "spent": deal["spent"],
            "proceeds": proceeds, "pnl": pnl, "reason": reason,
            "safety_orders_used": deal["safety_orders_used"],
        })
        del self.state["deals"][symbol]
        save_json(STATE_PATH, self.state)
        emoji = "✅" if pnl >= 0 else "🔴"
        self.notify(f"{emoji} CLOSED {symbol} ({reason}): PnL {pnl:+.2f} USDT "
                    f"| Cumulative: {self.state['realized_pnl']:+.2f} USDT")
        self._check_circuit_breaker()

    def _check_circuit_breaker(self):
        max_dd = self.cfg["total_capital_usdt"] * self.cfg["max_drawdown_pct"] / 100.0
        if self.state["realized_pnl"] <= -max_dd:
            self.state["halted"] = True
            save_json(STATE_PATH, self.state)
            self.notify(f"⛔ CIRCUIT BREAKER: cumulative loss "
                        f"{self.state['realized_pnl']:.2f} USDT exceeds "
                        f"{self.cfg['max_drawdown_pct']}% of capital. "
                        f"Trading HALTED. Review before restarting "
                        f"(delete 'halted' in state.json).")

    # ------------------------------------------------ per-tick logic
    def tick(self):
        if self.state.get("halted"):
            return
        for symbol in self.cfg["symbols"]:
            try:
                self._tick_symbol(symbol)
            except Exception as e:
                log.error("Error on %s: %s", symbol, e)

    def _tick_symbol(self, symbol):
        deal = self.state["deals"].get(symbol)
        px = self.price(symbol)

        if deal is None:
            if len(self.state["deals"]) < self.cfg["max_concurrent_deals"]:
                self.open_deal(symbol)
            return

        tp_price = deal["avg_entry"] * (1 + self.cfg["take_profit_pct"] / 100.0)
        sl_price = deal["avg_entry"] * (1 - self.cfg["stop_loss_pct"] / 100.0)
        so_price = deal["last_fill_price"] * (1 - deal["next_so_deviation_pct"] / 100.0)

        log.info("%s px=%.4f avg=%.4f tp=%.4f sl=%.4f next_so=%.4f (SO %d/%d)",
                 symbol, px, deal["avg_entry"], tp_price, sl_price, so_price,
                 deal["safety_orders_used"], self.cfg["max_safety_orders"])

        if px >= tp_price:
            self.close_deal(symbol, deal, "take_profit")
        elif px <= sl_price:
            self.close_deal(symbol, deal, "stop_loss")
        elif (px <= so_price
              and deal["safety_orders_used"] < self.cfg["max_safety_orders"]):
            self.add_safety_order(symbol, deal)

    # ------------------------------------------------ main loop
    def run(self):
        if self.paper:
            mode = "PAPER (simulated fills, live prices, no account)"
        else:
            mode = "TESTNET" if self.cfg.get("testnet", True) else "⚠️ LIVE ⚠️"
        self.notify(f"🤖 SafetyNet bot started [{mode}] on "
                    f"{', '.join(self.cfg['symbols'])}")
        running = True

        def stop(*_):
            nonlocal running
            running = False
            log.info("Shutdown signal received; finishing tick...")

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

        while running:
            self.tick()
            time.sleep(self.cfg["poll_interval_sec"])
        log.info("Bot stopped cleanly. State saved.")


def main():
    if not os.path.exists(CONFIG_PATH):
        log.error("config.json not found. Copy config.example.json to "
                  "config.json and fill in your Bybit TESTNET API keys.")
        sys.exit(1)
    cfg = load_json(CONFIG_PATH, None)
    if cfg.get("mode", "paper") == "live" and not cfg.get("testnet", True):
        log.warning("=" * 60)
        log.warning("LIVE MODE ENABLED — REAL MONEY AT RISK.")
        log.warning("Starting in 15 seconds. Ctrl+C to abort.")
        log.warning("=" * 60)
        time.sleep(15)
    bot = SafetyNetBot(cfg)
    if "--once" in sys.argv:
        bot.tick()
        log.info("Single tick complete (GitHub Actions mode). State saved.")
    else:
        bot.run()


if __name__ == "__main__":
    main()
