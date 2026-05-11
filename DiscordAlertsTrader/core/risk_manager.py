#!/usr/bin/env python3
"""
Risk management module for AlertsTrader.
Handles position sizing, risk limits, and trade validation.
"""
from typing import Dict, Any, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    """Trade risk limits configuration."""
    max_trade_capital: float = 5000.0
    min_trade_capital: float = 100.0
    max_position_size: int = 100
    max_daily_trades: int = 20
    max_strike: float = 400.0
    max_dte: int = 30
    min_premium: float = 0.30
    max_price_diff_pct: float = 5.0


@dataclass
class PositionSizing:
    """Position sizing configuration."""
    method: str = "buy_one"  # "buy_one", "trade_capital", "margin_capital"
    trade_capital: float = 300.0
    margin_capital: float = 20000.0
    risk_per_trade: float = 0.02  # 2% of account


class RiskManager:
    """
    Manages trading risk limits and position sizing.

    Responsibilities:
    - Validate trade against risk limits
    - Calculate position sizes
    - Enforce daily trade limits
    """

    def __init__(
        self,
        risk_limits: Optional[RiskLimits] = None,
        position_sizing: Optional[PositionSizing] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        self.risk_limits = risk_limits or RiskLimits()
        self.position_sizing = position_sizing or PositionSizing()
        self.config = config or {}
        self._daily_trade_count = 0

    def load_from_config(self, config: Dict[str, Any]) -> None:
        """Load risk settings from configuration dictionary."""
        order_config = config.get('order_configs', {})
        shorting_config = config.get('shorting', {})

        self.risk_limits.max_trade_capital = float(
            order_config.get('max_trade_capital', '5000').split(',')[-1].split(':')[-1].strip('} ')
        ) if isinstance(order_config.get('max_trade_capital'), str) else 5000.0

        self.risk_limits.min_trade_capital = float(shorting_config.get('min_trade_capital', '100'))
        self.risk_limits.max_strike = float(shorting_config.get('max_strike', '400'))
        self.risk_limits.max_dte = int(shorting_config.get('max_dte', '30'))
        self.risk_limits.min_premium = float(shorting_config.get('min_price', '0.30'))
        self.risk_limits.max_price_diff_pct = float(shorting_config.get('max_price_diff', '5'))

        self.position_sizing.method = shorting_config.get('default_sto_qty', 'buy_one')
        self.position_sizing.trade_capital = float(shorting_config.get('trade_capital', '300'))
        self.position_sizing.margin_capital = float(shorting_config.get('margin_capital', '20000'))

    def validate_short_strike(self, strike: float) -> tuple[bool, str]:
        """Validate short position strike price."""
        if strike > self.risk_limits.max_strike:
            return False, f"Strike {strike} exceeds maximum {self.risk_limits.max_strike}"
        return True, ""

    def validate_dte(self, dte: int) -> tuple[bool, str]:
        """Validate days to expiration."""
        if dte > self.risk_limits.max_dte:
            return False, f"DTE {dte} exceeds maximum {self.risk_limits.max_dte}"
        return True, ""

    def validate_premium(self, premium: float) -> tuple[bool, str]:
        """Validate option premium."""
        if premium * 100 < self.risk_limits.min_premium:
            return False, f"Premium {premium} below minimum {self.risk_limits.min_premium}"
        return True, ""

    def validate_price_diff(self, price_diff_pct: float) -> tuple[bool, str]:
        """Validate price difference percentage."""
        if price_diff_pct > self.risk_limits.max_price_diff_pct:
            return False, f"Price diff {price_diff_pct}% exceeds maximum {self.risk_limits.max_price_diff_pct}%"
        return True, ""

    def validate_trade_value(self, trade_value: float, asset_type: str = "option") -> tuple[bool, str]:
        """Validate total trade value against limits."""
        if asset_type == "option":
            if trade_value > self.risk_limits.max_trade_capital:
                return False, f"Trade value ${trade_value} exceeds maximum ${self.risk_limits.max_trade_capital}"
            if trade_value < self.risk_limits.min_trade_capital:
                return False, f"Trade value ${trade_value} below minimum ${self.risk_limits.min_trade_capital}"
        return True, ""

    def validate_daily_limit(self) -> tuple[bool, str]:
        """Check if daily trade limit has been reached."""
        if self._daily_trade_count >= self.risk_limits.max_daily_trades:
            return False, f"Daily limit of {self.risk_limits.max_daily_trades} trades reached"
        return True, ""

    def calculate_position_size(
        self,
        price: float,
        asset_type: str = "option",
        trader: Optional[str] = None,
        trader_overrides: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Calculate position size based on configured method.

        Args:
            price: Current price per unit
            asset_type: "stock" or "option"
            trader: Trader name for per-trader overrides
            trader_overrides: Trader-specific position sizing config

        Returns:
            Recommended position size (number of units)
        """
        method = self.position_sizing.method

        if trader_overrides and trader:
            method = trader_overrides.get(trader, method)

        if method == "buy_one":
            return 1

        elif method == "trade_capital":
            capital = self.position_sizing.trade_capital
            if asset_type == "option":
                return max(1, int(round(capital / (100 * price))))
            return max(1, int(capital // price))

        elif method == "margin_capital" and asset_type == "option":
            capital = self.position_sizing.margin_capital
            return max(1, int(capital / (20 * price)))

        return 1

    def validate_order(self, order: Dict[str, Any]) -> tuple[bool, str]:
        """
        Comprehensive order validation.

        Args:
            order: Order dictionary with keys like 'action', 'symbol', 'price', 'Qty', 'asset', etc.

        Returns:
            Tuple of (is_valid, error_message)
        """
        if order.get('action') == "STO" and order.get('asset') == "option":
            strike = order.get('strike', 0)
            if strike > 0:
                valid, msg = self.validate_short_strike(strike)
                if not valid:
                    return False, msg

            dte = order.get('dte', 0)
            if dte > 0:
                valid, msg = self.validate_dte(dte)
                if not valid:
                    return False, msg

            premium = order.get('price', 0)
            if premium > 0:
                valid, msg = self.validate_premium(premium)
                if not valid:
                    return False, msg

        qty = order.get('Qty', 1)
        price = order.get('price', 0)
        asset = order.get('asset', 'stock')
        trade_value = price * qty * (100 if asset == 'option' else 1)

        valid, msg = self.validate_trade_value(trade_value, asset)
        if not valid:
            return False, msg

        return True, ""

    def adjust_quantity(
        self,
        order: Dict[str, Any],
        target_value: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Adjust order quantity to meet risk limits.

        Args:
            order: Order dictionary to modify
            target_value: Target trade value (optional)

        Returns:
            Modified order dictionary
        """
        order = order.copy()
        qty = order.get('Qty', 1)
        price = order.get('price', 0)
        asset = order.get('asset', 'stock')

        current_value = price * qty * (100 if asset == 'option' else 1)

        if target_value is None:
            target_value = self.risk_limits.max_trade_capital

        if current_value > target_value:
            if price > 0:
                if asset == 'option':
                    new_qty = max(1, int(target_value // (100 * price)))
                else:
                    new_qty = max(1, int(target_value // price))
                order['Qty'] = new_qty
                order['adjusted_reason'] = 'exceeded_max_capital'

        return order

    def increment_daily_trades(self) -> None:
        """Increment the daily trade counter."""
        self._daily_trade_count += 1
        logger.debug(f"Daily trades: {self._daily_trade_count}/{self.risk_limits.max_daily_trades}")

    def reset_daily_trades(self) -> None:
        """Reset daily trade counter (call at start of trading day)."""
        self._daily_trade_count = 0
        logger.info("Daily trade counter reset")
