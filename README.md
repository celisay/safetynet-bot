# SafetyNet DCA Bot — Bybit (Paper Trading First)

A DCA "safety net" bot for Bybit spot, with hard capital-preservation rules built in. Ships in **paper mode by default**: simulated fills using live Bybit market prices — no exchange account, no API key, no registration required. Perfect if exchange websites are blocked on your local network, since the bot runs entirely on your VPS.

## How the strategy works

1. Opens a deal with a small **base order** (market buy).
2. If price drops, places up to N **safety orders** at widening intervals with increasing size, lowering your average entry.
3. Sells everything when price rises `take_profit_pct` above your **average entry**.
4. If price keeps falling past `stop_loss_pct` below average entry, it cuts the loss. No hoping, no bag-holding.

## Capital rules the bot enforces (it refuses to start if violated)

- Each deal (base + ALL safety orders) uses only `per_deal_budget_pct` of capital (default 3%).
- Worst-case loss per deal must be ≤ 2% of total capital.
- Total exposure across all deals must be ≤ 10% of capital (default config: 6%).
- Circuit breaker: if cumulative losses hit `max_drawdown_pct` (default 10%), the bot halts itself and notifies you.

## Modes

- `"mode": "paper"` (default) — no account needed. The bot fetches live prices from Bybit's public API and simulates buys/sells internally with a 0.1% fee, tracking a virtual USDT balance in `state.json`. Results are as close to real as a simulator gets (minus real slippage).
- `"mode": "live"` — requires a Bybit account and API key. Only switch after the go-live checklist below.

## Setup (paper trading — no registration needed)

1. On your VPS (Ubuntu):
   ```bash
   sudo apt update && sudo apt install -y python3-pip python3-venv
   mkdir ~/safetynet-bot && cd ~/safetynet-bot
   # upload the files here (scp or paste)
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   cp config.example.json config.json
   python3 bot.py     # works immediately — no keys needed in paper mode
   ```
2. (Optional) Telegram alerts: create a bot with @BotFather, get your chat ID from @userinfobot, and fill both fields in config.json.

## Run 24/7 with systemd

```bash
sudo cp safetynet-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/safetynet-bot.service  # fix paths/username
sudo systemctl daemon-reload
sudo systemctl enable --now safetynet-bot
journalctl -u safetynet-bot -f   # watch logs
```

## Config reference

| Key | Meaning | Default |
|---|---|---|
| `total_capital_usdt` | Your total trading capital (the bot sizes everything from this) | 1000 |
| `per_deal_budget_pct` | Max % of capital one deal can ever use | 3.0 |
| `max_concurrent_deals` | Max simultaneous deals (= symbols trading at once) | 2 |
| `take_profit_pct` | Profit target measured from average entry | 1.5 |
| `stop_loss_pct` | Hard stop measured from average entry | 12.0 |
| `max_safety_orders` | Safety orders per deal | 4 |
| `safety_order_deviation_pct` | Price drop from last fill to trigger the next SO | 1.5 |
| `safety_order_step_scale` | Each SO trigger gap grows by this factor | 1.4 |
| `safety_order_volume_scale` | Each SO size grows by this factor | 1.5 |
| `max_drawdown_pct` | Circuit breaker: cumulative loss that halts the bot | 10.0 |
| `poll_interval_sec` | Seconds between price checks | 30 |

With defaults: SOs trigger at roughly -1.5%, -3.6%, -6.5%, -10.6% from entry; the stop-loss at -12% from average entry means worst case ≈ **0.36% of capital lost per deal**.

## Go-live checklist (do NOT skip)

- [ ] 4+ weeks in paper mode with the exact config you plan to use
- [ ] Review `state.json` closed_deals: win rate, average win vs average loss
- [ ] Stop-loss fired at least once and behaved correctly
- [ ] Circuit breaker logic understood
- [ ] Live API key: Read + Spot Trade ONLY, IP-whitelisted to your VPS
- [ ] Start live with small capital you can fully afford to lose
- [ ] Set `"mode": "live"` and `"testnet": false` — the bot gives you a 15-second abort window
- [ ] Note for Nigeria: exchange websites are ISP-blocked locally and foreign exchanges lack SEC Nigeria VASP licences — consider a locally licensed exchange for live trading, or keep funds/withdrawal paths carefully planned

## Honest limitations

- This is a mean-reversion strategy. It bleeds in strong sustained downtrends (the stop-loss caps each bleed, but repeated stop-outs add up — that's what the circuit breaker is for).
- Market orders mean small slippage on each fill.
- Nothing here is financial advice. Past performance of any strategy does not guarantee future results.
