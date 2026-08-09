# BTC/USDT Arbitrage Monitor

A read-only Python monitor for the Binance and Bybit BTC/USDT spot markets.
It reads direct public order-book APIs from Binance Spot and Bybit Spot,
uses executable best bid/ask prices, evaluates both arbitrage directions,
subtracts a fixed estimated **0.10% buy fee + 0.10% sell fee**, and sends a
Telegram alert when the net spread is at least **0.30%**.

It does not execute trades. It has no exchange API keys, order endpoints,
withdrawal endpoints, or trading permissions.

## How the estimate works

For each direction, the monitor assumes:

1. Buy BTC at the cheaper exchange's best ask.
2. Sell BTC at the other exchange's best bid.
3. Pay the estimated 0.10% fee on both sides.

The net spread is calculated as:

```text
(sell_bid * (1 - sell_fee)) / (buy_ask * (1 + buy_fee)) - 1
```

This is still an estimate and does not account for slippage beyond the
top-of-book quote, transfer time, withdrawal/deposit fees, limits, inventory,
funding, taxes, or execution risk.

The monitor uses official direct public API hosts and host variants only. It
does not use CoinPaprika, exchange API keys, or any order/trading endpoint.

## Configuration

The monitor reads:

- `TELEGRAM_BOT_TOKEN` — secure secret used only for Telegram `sendMessage`.
- `TELEGRAM_CHAT_ID` — destination chat, group, or channel ID.
- `ARBITRAGE_THRESHOLD_PCT` — defaults to `0.30`; values below `0.30` are rejected.
- `POLL_INTERVAL_SECONDS` — defaults to `5` seconds.
- `ALERT_COOLDOWN_SECONDS` — defaults to `300`; prevents repeated alerts for a
  persistent opportunity.
- `HTTP_TIMEOUT_SECONDS` — defaults to `8`.

## Run

From the project root:

```bash
python3 crypto-arbitrage-monitor/monitor.py
```

For one safe smoke-test poll without Telegram delivery:

```bash
python3 crypto-arbitrage-monitor/monitor.py --once --no-telegram
```

To send exactly one Telegram connectivity test to the configured
`TELEGRAM_CHAT_ID`:

```bash
python3 crypto-arbitrage-monitor/monitor.py --test-telegram
```

This command does not poll market data, calculate spreads, or execute trades.
It sends only:

```text
🚨 MOFFAT ARBITRAGE BOT TEST — Telegram connection is working.
```

To diagnose direct public market-data access without Telegram credentials:

```bash
python3 crypto-arbitrage-monitor/monitor.py --test-market-data
```

This prints the endpoint, HTTP status, and Binance/Bybit best bid/ask values
when available. The Bybit endpoint is the official V5 Spot ticker:

```text
https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT
```

If the runtime is blocked by Bybit's network edge, the command reports the
HTTP status and body detail and exits without substituting another data source.

Run the built-in offline math tests:

```bash
python3 -m unittest discover -s crypto-arbitrage-monitor -p 'test_*.py'
```

## External runtime deployment

The monitor uses only Python 3's standard library and can run on an external
VPS or another unrestricted runtime. Use
[`VPS_DEPLOYMENT.md`](VPS_DEPLOYMENT.md) for environment-file and systemd
instructions. The included `run.sh` launcher is suitable for process managers.
Keep `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the runtime environment;
never commit or print their values.

The process handles Ctrl+C and termination signals cleanly. If either
exchange's public quote is unavailable, that polling cycle is skipped rather
than producing an incomplete or misleading alert.