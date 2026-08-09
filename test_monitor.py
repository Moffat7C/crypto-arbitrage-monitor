#!/usr/bin/env python3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from monitor import (
    Config,
    BYBIT_TICKER_URL,
    MarketDataProbe,
    TELEGRAM_TEST_MESSAGE,
    Quote,
    _parse_binance_book_ticker,
    _parse_bybit_ticker,
    calculate_opportunity,
    format_opportunity_alert,
    send_telegram_test,
)


class ArbitrageMathTests(unittest.TestCase):
    def setUp(self) -> None:
        observed_at = datetime.now(timezone.utc)
        self.binance = Quote("Binance Spot", bid=100_000.0, ask=100_010.0, observed_at=observed_at)
        self.bybit = Quote("Bybit Spot", bid=100_500.0, ask=100_510.0, observed_at=observed_at)

    def test_net_spread_accounts_for_both_taker_fees(self) -> None:
        opportunity = calculate_opportunity(
            self.binance,
            self.bybit,
            buy_fee_pct=0.10,
            sell_fee_pct=0.10,
        )
        self.assertAlmostEqual(opportunity.gross_spread_pct, 0.489951, places=4)
        self.assertAlmostEqual(opportunity.estimated_fee_drag_pct, 0.199800, places=4)
        self.assertAlmostEqual(opportunity.net_spread_pct, 0.289173, places=4)
        self.assertEqual(opportunity.key, "Binance Spot->Bybit Spot")

    def test_alert_message_is_explicitly_read_only(self) -> None:
        opportunity = calculate_opportunity(
            self.binance,
            self.bybit,
            buy_fee_pct=0.10,
            sell_fee_pct=0.10,
        )
        message = format_opportunity_alert(opportunity, threshold_pct=0.30)
        self.assertIn("Read-only monitor; no trades executed.", message)
        self.assertIn("Net spread:", message)
        self.assertIn("Buy ask price:", message)
        self.assertIn("Sell bid price:", message)
        self.assertIn("Read-only monitor; no trades executed.", message)

    def test_binance_book_ticker_parser_uses_best_bid_and_ask(self) -> None:
        self.assertEqual(
            _parse_binance_book_ticker(
                {"symbol": "BTCUSDT", "bidPrice": "64922.50", "askPrice": "64922.60"}
            ),
            (64922.50, 64922.60),
        )

    def test_bybit_ticker_parser_uses_spot_best_bid_and_ask(self) -> None:
        self.assertEqual(
            _parse_bybit_ticker(
                {
                    "retCode": 0,
                    "result": {
                        "category": "spot",
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "bid1Price": "64922.50",
                                "ask1Price": "64922.60",
                            }
                        ],
                    },
                }
            ),
            (64922.50, 64922.60),
        )

    def test_bybit_ticker_parser_rejects_missing_ticker(self) -> None:
        with self.assertRaises(Exception):
            _parse_bybit_ticker({"retCode": 0, "result": {"list": []}})

    def test_market_data_probe_preserves_http_403_diagnostic(self) -> None:
        blocked = MarketDataProbe(
            exchange="Bybit Spot",
            url=BYBIT_TICKER_URL,
            http_status=403,
            bid=None,
            ask=None,
            detail="HTTP 403: CloudFront blocked access from this country",
        )
        self.assertEqual(blocked.http_status, 403)
        self.assertIn("blocked access", blocked.detail)

    def test_telegram_test_message_is_exact_and_requires_configuration(self) -> None:
        self.assertEqual(
            TELEGRAM_TEST_MESSAGE,
            "🚨 MOFFAT ARBITRAGE BOT TEST — Telegram connection is working.",
        )
        config = Config(
            telegram_bot_token="test-token",
            telegram_chat_id="test-chat",
            threshold_pct=0.30,
            poll_interval_seconds=15.0,
            alert_cooldown_seconds=300.0,
            http_timeout_seconds=8.0,
        )
        with patch("monitor.send_telegram_message") as send_message:
            send_telegram_test(config)
        send_message.assert_called_once_with(
            "test-token",
            "test-chat",
            TELEGRAM_TEST_MESSAGE,
            timeout_seconds=8.0,
        )

        missing_config = Config(
            telegram_bot_token=None,
            telegram_chat_id=None,
            threshold_pct=0.30,
            poll_interval_seconds=15.0,
            alert_cooldown_seconds=300.0,
            http_timeout_seconds=8.0,
        )
        with self.assertRaises(ValueError):
            send_telegram_test(missing_config)


if __name__ == "__main__":
    unittest.main()