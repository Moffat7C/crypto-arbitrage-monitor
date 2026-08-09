#!/usr/bin/env python3
"""Read-only BTC/USDT arbitrage monitor using direct public exchange data.

This program only reads public Binance Spot and Bybit Spot order-book data and
sends Telegram messages. It never calls a trading endpoint and never places
orders.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("crypto-arbitrage-monitor")
SYMBOL = "BTC/USDT"
BUY_FEE_PCT = 0.10
SELL_FEE_PCT = 0.10
BINANCE_BOOK_TICKER_URLS = (
    "https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT",
    "https://api-gcp.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT",
    "https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=BTCUSDT",
    "https://api1.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT",
    "https://api2.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT",
    "https://api3.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT",
)
BYBIT_TICKER_URL = (
    "https://api.bytick.com/v5/market/tickers?category=spot&symbol=BTCUSDT"
)
TELEGRAM_TEST_MESSAGE = (
    "🚨 MOFFAT ARBITRAGE BOT TEST — Telegram connection is working."
)


class ConfigurationError(ValueError):
    """Raised when required configuration is missing or invalid."""


class ExchangeDataError(RuntimeError):
    """Raised when a public exchange response cannot be used."""


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    threshold_pct: float
    poll_interval_seconds: float
    alert_cooldown_seconds: float
    http_timeout_seconds: float


@dataclass(frozen=True)
class Quote:
    exchange: str
    bid: float
    ask: float
    observed_at: datetime


@dataclass(frozen=True)
class MarketDataProbe:
    exchange: str
    url: str
    http_status: int | None
    bid: float | None
    ask: float | None
    detail: str


@dataclass(frozen=True)
class Opportunity:
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    buy_fee_pct: float
    sell_fee_pct: float
    gross_spread_pct: float
    estimated_fee_drag_pct: float
    net_spread_pct: float

    @property
    def key(self) -> str:
        return f"{self.buy_exchange}->{self.sell_exchange}"


def _read_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and at most {maximum}" if maximum is not None else ""
        raise ConfigurationError(f"{name} must be at least {minimum}{upper}")
    return value


def load_config(*, require_telegram: bool = True) -> Config:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
    if require_telegram and not token:
        raise ConfigurationError("TELEGRAM_BOT_TOKEN is required unless --no-telegram is used")
    if require_telegram and not chat_id:
        raise ConfigurationError("TELEGRAM_CHAT_ID is required unless --no-telegram is used")

    threshold_pct = _read_float(
        "ARBITRAGE_THRESHOLD_PCT",
        0.30,
        minimum=0.30,
    )
    return Config(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        threshold_pct=threshold_pct,
        poll_interval_seconds=_read_float(
            "POLL_INTERVAL_SECONDS",
            5.0,
            minimum=1.0,
        ),
        alert_cooldown_seconds=_read_float(
            "ALERT_COOLDOWN_SECONDS",
            300.0,
            minimum=0.0,
        ),
        http_timeout_seconds=_read_float(
            "HTTP_TIMEOUT_SECONDS",
            8.0,
            minimum=1.0,
            maximum=60.0,
        ),
    )


def _compact_body(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").replace("\n", " ").strip()
    return text[:300] + ("..." if len(text) > 300 else "")


def _http_get_json(url: str, *, timeout_seconds: float) -> tuple[int | None, Any, str]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "crypto-arbitrage-monitor/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
            return response.status, payload, _compact_body(body)
    except HTTPError as exc:
        body = exc.read()
        return exc.code, None, _compact_body(body) or str(exc)
    except (URLError, TimeoutError, OSError) as exc:
        return None, None, str(exc)


def _request_json(url: str, *, timeout_seconds: float) -> Any:
    status, payload, detail = _http_get_json(
        url,
        timeout_seconds=timeout_seconds,
    )
    if status != 200:
        status_text = str(status) if status is not None else "unavailable"
        raise ExchangeDataError(f"HTTP {status_text} from {url}: {detail}")
    if payload is None:
        raise ExchangeDataError(f"public market-data API returned invalid JSON: {detail}")
    return payload


def _request_json_with_fallbacks(
    urls: tuple[str, ...],
    *,
    timeout_seconds: float,
) -> Any:
    last_error: ExchangeDataError | None = None
    for url in urls:
        try:
            return _request_json(url, timeout_seconds=timeout_seconds)
        except ExchangeDataError as exc:
            last_error = exc
            LOGGER.debug("Public endpoint unavailable (%s): %s", url, exc)
    assert last_error is not None
    raise last_error


def _positive_price(value: Any, field_name: str) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ExchangeDataError(f"{field_name} was not numeric") from exc
    if price <= 0:
        raise ExchangeDataError(f"{field_name} was not positive")
    return price


def _parse_binance_book_ticker(payload: Any) -> tuple[float, float]:
    if not isinstance(payload, dict):
        raise ExchangeDataError("Binance returned an unexpected bookTicker payload")
    return (
        _positive_price(payload.get("bidPrice"), "Binance best bid"),
        _positive_price(payload.get("askPrice"), "Binance best ask"),
    )


def _parse_bybit_ticker(payload: Any) -> tuple[float, float]:
    if not isinstance(payload, dict):
        raise ExchangeDataError("Bybit returned an unexpected ticker payload")
    if payload.get("retCode") not in (None, 0):
        raise ExchangeDataError(
            f"Bybit returned error {payload.get('retCode')}: {payload.get('retMsg', 'unknown error')}"
        )
    result = payload.get("result")
    rows = result.get("list") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ExchangeDataError("Bybit returned no BTCUSDT spot ticker")
    row = rows[0]
    return (
        _positive_price(row.get("bid1Price"), "Bybit best bid"),
        _positive_price(row.get("ask1Price"), "Bybit best ask"),
    )


def fetch_binance_quote(timeout_seconds: float) -> Quote:
    payload = _request_json_with_fallbacks(
        BINANCE_BOOK_TICKER_URLS,
        timeout_seconds=timeout_seconds,
    )
    bid, ask = _parse_binance_book_ticker(payload)
    return Quote(
        exchange="Binance Spot",
        bid=bid,
        ask=ask,
        observed_at=datetime.now(timezone.utc),
    )


def fetch_bybit_quote(timeout_seconds: float) -> Quote:
    payload = _request_json(
        BYBIT_TICKER_URL,
        timeout_seconds=timeout_seconds,
    )
    bid, ask = _parse_bybit_ticker(payload)
    return Quote(
        exchange="Bybit Spot",
        bid=bid,
        ask=ask,
        observed_at=datetime.now(timezone.utc),
    )


def _probe_endpoint(
    exchange: str,
    url: str,
    parser: Callable[[Any], tuple[float, float]],
    timeout_seconds: float,
) -> MarketDataProbe:
    status, payload, detail = _http_get_json(url, timeout_seconds=timeout_seconds)
    if status != 200:
        status_text = str(status) if status is not None else "unavailable"
        return MarketDataProbe(
            exchange=exchange,
            url=url,
            http_status=status,
            bid=None,
            ask=None,
            detail=f"HTTP {status_text}: {detail}",
        )
    try:
        bid, ask = parser(payload)
    except (ExchangeDataError, ValueError, TypeError) as exc:
        return MarketDataProbe(
            exchange=exchange,
            url=url,
            http_status=status,
            bid=None,
            ask=None,
            detail=str(exc),
        )
    return MarketDataProbe(
        exchange=exchange,
        url=url,
        http_status=status,
        bid=bid,
        ask=ask,
        detail="OK",
    )


def probe_market_data(timeout_seconds: float) -> tuple[MarketDataProbe, ...]:
    return (
        _probe_endpoint(
            "Binance Spot",
            BINANCE_BOOK_TICKER_URLS[0],
            _parse_binance_book_ticker,
            timeout_seconds,
        ),
        _probe_endpoint(
            "Bybit Spot",
            BYBIT_TICKER_URL,
            _parse_bybit_ticker,
            timeout_seconds,
        ),
    )


def print_market_data_test(timeout_seconds: float) -> int:
    probes = probe_market_data(timeout_seconds)
    all_available = True
    for probe in probes:
        print(probe.exchange)
        print(f"Endpoint: {probe.url}")
        print(
            "HTTP status: "
            + (str(probe.http_status) if probe.http_status is not None else "unavailable")
        )
        if probe.bid is None or probe.ask is None:
            all_available = False
            print("Bid: unavailable")
            print("Ask: unavailable")
        else:
            print(f"Bid: {probe.bid:.8f}")
            print(f"Ask: {probe.ask:.8f}")
        print(f"Result: {probe.detail}")
        print()
    return 0 if all_available else 1


def calculate_opportunity(
    buy_quote: Quote,
    sell_quote: Quote,
    *,
    buy_fee_pct: float,
    sell_fee_pct: float,
) -> Opportunity:
    if buy_quote.ask <= 0 or sell_quote.bid <= 0:
        raise ValueError("quote prices must be positive")
    gross_ratio = sell_quote.bid / buy_quote.ask
    fee_multiplier = (1.0 - sell_fee_pct / 100.0) / (1.0 + buy_fee_pct / 100.0)
    return Opportunity(
        buy_exchange=buy_quote.exchange,
        sell_exchange=sell_quote.exchange,
        buy_price=buy_quote.ask,
        sell_price=sell_quote.bid,
        buy_fee_pct=buy_fee_pct,
        sell_fee_pct=sell_fee_pct,
        gross_spread_pct=(gross_ratio - 1.0) * 100.0,
        estimated_fee_drag_pct=(1.0 - fee_multiplier) * 100.0,
        net_spread_pct=(gross_ratio * fee_multiplier - 1.0) * 100.0,
    )


def find_opportunities(quotes: dict[str, Quote], config: Config) -> list[Opportunity]:
    binance = quotes["Binance Spot"]
    bybit = quotes["Bybit Spot"]
    return [
        calculate_opportunity(
            binance,
            bybit,
            buy_fee_pct=BUY_FEE_PCT,
            sell_fee_pct=SELL_FEE_PCT,
        ),
        calculate_opportunity(
            bybit,
            binance,
            buy_fee_pct=BUY_FEE_PCT,
            sell_fee_pct=SELL_FEE_PCT,
        ),
    ]


def format_opportunity_alert(opportunity: Opportunity, *, threshold_pct: float) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        "BTC/USDT arbitrage opportunity\n"
        f"Buy exchange: {opportunity.buy_exchange}\n"
        f"Buy ask price: ${opportunity.buy_price:,.2f}\n"
        f"Sell exchange: {opportunity.sell_exchange}\n"
        f"Sell bid price: ${opportunity.sell_price:,.2f}\n"
        f"Gross spread: {opportunity.gross_spread_pct:.3f}%\n"
        f"Estimated fees: {opportunity.estimated_fee_drag_pct:.3f}% total "
        f"({opportunity.buy_fee_pct:.3f}% buy + {opportunity.sell_fee_pct:.3f}% sell)\n"
        f"Net spread: {opportunity.net_spread_pct:.3f}% "
        f"(alert threshold: {threshold_pct:.3f}%)\n"
        f"Timestamp: {timestamp}\n"
        "Read-only monitor; no trades executed."
    )


def send_telegram_message(token: str, chat_id: str, message: str, *, timeout_seconds: float) -> None:
    body = urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "crypto-arbitrage-monitor/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Telegram request failed: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        description = payload.get("description", "unknown Telegram error") if isinstance(payload, dict) else "invalid response"
        raise RuntimeError(f"Telegram rejected the alert: {description}")


def send_telegram_test(config: Config) -> None:
    if not config.telegram_bot_token or not config.telegram_chat_id:
        raise ConfigurationError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required for --test-telegram"
        )
    send_telegram_message(
        config.telegram_bot_token,
        config.telegram_chat_id,
        TELEGRAM_TEST_MESSAGE,
        timeout_seconds=config.http_timeout_seconds,
    )


class AlertTracker:
    """Suppresses duplicate alerts while allowing periodic reminders."""

    def __init__(self, cooldown_seconds: float) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._last_sent: dict[str, float] = {}

    def should_send(self, key: str, now: float) -> bool:
        last_sent = self._last_sent.get(key)
        return last_sent is None or now - last_sent >= self.cooldown_seconds

    def mark_sent(self, key: str, now: float) -> None:
        self._last_sent[key] = now


def poll_quotes(config: Config) -> dict[str, Quote]:
    fetchers: dict[str, Callable[[float], Quote]] = {
        "Binance Spot": fetch_binance_quote,
        "Bybit Spot": fetch_bybit_quote,
    }
    quotes: dict[str, Quote] = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="quote") as executor:
        futures = {
            executor.submit(fetcher, config.http_timeout_seconds): exchange
            for exchange, fetcher in fetchers.items()
        }
        for future in as_completed(futures):
            exchange = futures[future]
            try:
                quotes[exchange] = future.result()
            except (ExchangeDataError, ValueError, TypeError) as exc:
                LOGGER.warning("%s quote unavailable: %s", exchange, exc)
    return quotes


def log_opportunity(opportunity: Opportunity, threshold_pct: float) -> None:
    LOGGER.info(
        "%s buy on %s at %.2f, sell on %s at %.2f, gross %.3f%%, "
        "fee drag %.3f%%, net %.3f%%, threshold %.3f%%",
        SYMBOL,
        opportunity.buy_exchange,
        opportunity.buy_price,
        opportunity.sell_exchange,
        opportunity.sell_price,
        opportunity.gross_spread_pct,
        opportunity.estimated_fee_drag_pct,
        opportunity.net_spread_pct,
        threshold_pct,
    )


def run_monitor(config: Config, *, once: bool, send_alerts: bool) -> None:
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True
        LOGGER.info("Shutdown requested")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    tracker = AlertTracker(config.alert_cooldown_seconds)

    while not stopping:
        started = time.monotonic()
        quotes = poll_quotes(config)
        if len(quotes) == 2:
            opportunities = find_opportunities(quotes, config)
            for opportunity in opportunities:
                log_opportunity(opportunity, config.threshold_pct)
                if opportunity.net_spread_pct < config.threshold_pct:
                    continue
                now = time.monotonic()
                if not tracker.should_send(opportunity.key, now):
                    LOGGER.info("Alert suppressed for %s (cooldown active)", opportunity.key)
                    continue
                message = format_opportunity_alert(
                    opportunity,
                    threshold_pct=config.threshold_pct,
                )
                if not send_alerts:
                    LOGGER.info("Telegram disabled; would send:\n%s", message)
                    tracker.mark_sent(opportunity.key, now)
                else:
                    assert config.telegram_bot_token and config.telegram_chat_id
                    try:
                        send_telegram_message(
                            config.telegram_bot_token,
                            config.telegram_chat_id,
                            message,
                            timeout_seconds=config.http_timeout_seconds,
                        )
                    except RuntimeError as exc:
                        LOGGER.error("Could not send Telegram alert: %s", exc)
                    else:
                        LOGGER.info("Telegram alert sent for %s", opportunity.key)
                        tracker.mark_sent(opportunity.key, now)
        else:
            LOGGER.warning("Skipping spread calculation because an exchange quote is missing")

        if once:
            return
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, config.poll_interval_seconds - elapsed))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor read-only BTC/USDT arbitrage between Binance and Bybit."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="poll once and exit",
    )
    mode.add_argument(
        "--test-telegram",
        action="store_true",
        help="send one Telegram connection test message and exit without polling markets",
    )
    mode.add_argument(
        "--test-market-data",
        action="store_true",
        help="print Binance and Bybit public quote status and bid/ask, then exit",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="do not send alerts; useful for a safe local smoke test",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=os.getenv("LOG_LEVEL", "INFO").upper(),
        help="logging verbosity (default: INFO)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.test_telegram and args.no_telegram:
        LOGGER.error("--test-telegram cannot be combined with --no-telegram")
        return 2
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        config = load_config(
            require_telegram=not (
                args.no_telegram or args.test_market_data
            )
        )
    except ConfigurationError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return 2

    if args.test_market_data:
        return print_market_data_test(config.http_timeout_seconds)

    if args.test_telegram:
        try:
            send_telegram_test(config)
        except (ConfigurationError, RuntimeError) as exc:
            LOGGER.error("Telegram test failed: %s", exc)
            return 1
        LOGGER.info("Telegram test message sent successfully")
        return 0

    LOGGER.info(
        "Starting read-only %s monitor via direct public Binance/Bybit APIs; "
        "buy fee %.2f%%, sell fee %.2f%%; "
        "threshold %.3f%%; poll interval %.1fs",
        SYMBOL,
        BUY_FEE_PCT,
        SELL_FEE_PCT,
        config.threshold_pct,
        config.poll_interval_seconds,
    )
    run_monitor(config, once=args.once, send_alerts=not args.no_telegram)
    return 0


if __name__ == "__main__":
    sys.exit(main())
