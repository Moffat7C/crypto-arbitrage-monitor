# External VPS / Unrestricted Runtime Deployment

This monitor is intentionally portable: it uses only the Python 3 standard
library and direct public Binance Spot / Bybit Spot market-data APIs. It does
not require exchange API keys or any trading permissions.

Use a VPS or runtime whose outbound network location is permitted by both
exchanges. The Replit development runtime currently receives location-based
blocks from the exchanges, so it cannot provide a complete live quote pair.

## Required environment variables

Set these in the runtime environment or in a protected environment file:

```text
TELEGRAM_BOT_TOKEN=<your Telegram bot token>
TELEGRAM_CHAT_ID=<your Telegram destination chat ID>
```

Optional settings are documented in `.env.example`:

- `ARBITRAGE_THRESHOLD_PCT` — default `0.30`; values below `0.30` are rejected.
- `POLL_INTERVAL_SECONDS` — default `5`.
- `ALERT_COOLDOWN_SECONDS` — default `300`.
- `HTTP_TIMEOUT_SECONDS` — default `8`.

Never commit the real values, pass the bot token in a shell command, or print
the environment. The monitor never logs the token.

## Manual run

From the repository root on the external runtime:

```bash
python3 crypto-arbitrage-monitor/monitor.py --test-market-data
python3 -m unittest discover -s crypto-arbitrage-monitor -p 'test_*.py'
python3 crypto-arbitrage-monitor/monitor.py
```

The first command must show HTTP 200 and bid/ask values for both exchanges
before the continuous monitor can calculate spreads. If either exchange is
blocked, it reports the HTTP status and skips that cycle; it never substitutes
last-traded or aggregator prices.

To verify Telegram separately:

```bash
python3 crypto-arbitrage-monitor/monitor.py --test-telegram
```

That sends exactly one fixed test message and does not poll market data or
execute trades.

## systemd installation

The included `systemd/crypto-arbitrage-monitor.service` assumes the project is
installed at `/opt/crypto-arbitrage-monitor`. Adjust the paths if needed.

```bash
sudo useradd --system --home /var/lib/crypto-arbitrage-monitor \
  --shell /usr/sbin/nologin crypto-monitor
sudo mkdir -p /opt/crypto-arbitrage-monitor /etc/crypto-arbitrage-monitor
sudo cp -a . /opt/crypto-arbitrage-monitor/
sudo chown -R root:root /opt/crypto-arbitrage-monitor
sudo install -d -o crypto-monitor -g crypto-monitor \
  /var/lib/crypto-arbitrage-monitor
```

Create the protected environment file:

```bash
sudo sh -c 'umask 077; cat > /etc/crypto-arbitrage-monitor/monitor.env' <<'EOF'
TELEGRAM_BOT_TOKEN=<your Telegram bot token>
TELEGRAM_CHAT_ID=<your Telegram destination chat ID>
ARBITRAGE_THRESHOLD_PCT=0.30
POLL_INTERVAL_SECONDS=5
ALERT_COOLDOWN_SECONDS=300
HTTP_TIMEOUT_SECONDS=8
EOF
sudo chown root:crypto-monitor /etc/crypto-arbitrage-monitor/monitor.env
sudo chmod 0640 /etc/crypto-arbitrage-monitor/monitor.env
```

Install and start the service:

```bash
sudo cp crypto-arbitrage-monitor/systemd/crypto-arbitrage-monitor.service \
  /etc/systemd/system/crypto-arbitrage-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-arbitrage-monitor
sudo systemctl status crypto-arbitrage-monitor
sudo journalctl -u crypto-arbitrage-monitor -f
```

## Security boundaries

- Only public market-data GET requests go to Binance and Bybit.
- The only authenticated request is Telegram `sendMessage`.
- There are no exchange order, trade, withdrawal, account, or API-key paths.
- The process fails closed if either best bid/ask quote is unavailable.
- Telegram alerts are threshold-gated and cooldown-protected.