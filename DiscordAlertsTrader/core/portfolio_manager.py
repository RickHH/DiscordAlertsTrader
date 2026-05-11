#!/usr/bin/env python3
"""
Portfolio management module for AlertsTrader.
Handles portfolio state, trade logging, and performance tracking.
"""
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime
import pandas as pd
import logging
import os

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """Represents a single trade in the portfolio."""
    date: datetime
    symbol: str
    action: str  # BTO, STC, STO, BTC
    quantity: int
    price: float
    is_open: bool = True
    asset: str = "stock"
    trader: Optional[str] = None
    channel: Optional[str] = None
    order_id: Optional[str] = None
    status: str = "PENDING"
    exit_plan: Optional[str] = None
    pnl: Optional[float] = None
    pnl_dollar: Optional[float] = None
    price_alert: Optional[float] = None
    price_actual: Optional[float] = None
    filled_quantity: Optional[int] = None
    risk: Optional[str] = None
    avg_price: Optional[float] = None
    underlying: Optional[str] = None
    exp_date: Optional[str] = None
    strike: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for CSV storage."""
        return {
            'Date': self.date.strftime('%Y-%m-%d %H:%M:%S.%f') if self.date else None,
            'Symbol': self.symbol,
            'Type': self.action,
            'Qty': self.quantity,
            'Price': self.price,
            'isOpen': 1 if self.is_open else 0,
            'Asset': self.asset,
            'Trader': self.trader,
            'Channel': self.channel,
            'ordID': self.order_id,
            'BTO-Status': self.status,
            'exit_plan': self.exit_plan,
            'PnL': self.pnl,
            'PnL$': self.pnl_dollar,
            'Price-alert': self.price_alert,
            'Price-actual': self.price_actual,
            'filledQty': self.filled_quantity,
            'Risk': self.risk,
            'underlying': self.underlying,
            'expDate': self.exp_date,
            'strike': self.strike,
        }


@dataclass
class PortfolioStats:
    """Portfolio performance statistics."""
    total_trades: int = 0
    open_trades: int = 0
    closed_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    def calculate(self, portfolio: pd.DataFrame) -> 'PortfolioStats':
        """Calculate stats from portfolio DataFrame."""
        if portfolio.empty:
            return self

        closed = portfolio[portfolio['isOpen'] == 0]
        self.total_trades = len(portfolio)
        self.open_trades = len(portfolio[portfolio['isOpen'] == 1])
        self.closed_trades = len(closed)

        if self.closed_trades > 0:
            wins = closed[closed['PnL$'] > 0]
            losses = closed[closed['PnL$'] <= 0]

            self.winning_trades = len(wins)
            self.losing_trades = len(losses)
            self.win_rate = self.winning_trades / self.closed_trades * 100
            self.total_pnl = closed['PnL$'].sum()

            if self.winning_trades > 0:
                self.avg_win = wins['PnL$'].mean()
                self.largest_win = wins['PnL$'].max()

            if self.losing_trades > 0:
                self.avg_loss = losses['PnL$'].mean()
                self.largest_loss = abs(losses['PnL$'].min())

        return self


class PortfolioManager:
    """
    Manages portfolio state and operations.

    Responsibilities:
    - Load/save portfolio data
    - Add/update trades
    - Track open positions
    - Calculate performance statistics
    """

    def __init__(
        self,
        portfolio_fname: str,
        column_names: Optional[List[str]] = None
    ):
        self.portfolio_fname = portfolio_fname
        self.column_names = column_names or []
        self._portfolio: Optional[pd.DataFrame] = None
        self._dirty = False
        self._load_portfolio()

    def _load_portfolio(self) -> None:
        """Load portfolio from CSV file."""
        if os.path.exists(self.portfolio_fname):
            self._portfolio = pd.read_csv(self.portfolio_fname, na_values=[''])
        else:
            self._portfolio = pd.DataFrame(
                columns=self.column_names if self.column_names else self._default_columns()
            )
            self._save_portfolio()

    def _default_columns(self) -> List[str]:
        """Default portfolio columns."""
        return [
            'Date', 'Symbol', 'Type', 'Qty', 'Price', 'isOpen', 'Asset',
            'Trader', 'Channel', 'ordID', 'BTO-Status', 'exit_plan',
            'PnL', 'PnL$', 'Price-alert', 'Price-actual', 'filledQty',
            'Risk', 'underlying', 'expDate', 'strike'
        ]

    def _save_portfolio(self) -> None:
        """Save portfolio to CSV file."""
        if self._portfolio is not None:
            self._portfolio.to_csv(self.portfolio_fname, index=False)
            self._dirty = False
            logger.debug(f"Saved portfolio to {self.portfolio_fname}")

    @property
    def portfolio(self) -> pd.DataFrame:
        """Get portfolio DataFrame."""
        return self._portfolio

    @portfolio.setter
    def portfolio(self, df: pd.DataFrame) -> None:
        """Set portfolio DataFrame."""
        self._portfolio = df
        self._dirty = True

    def save_if_dirty(self) -> None:
        """Save portfolio only if modified."""
        if self._dirty:
            self._save_portfolio()

    def mark_dirty(self) -> None:
        """Mark portfolio as modified."""
        self._dirty = True

    def add_trade(self, trade: Trade) -> int:
        """
        Add a new trade to portfolio.

        Returns:
            Index of new trade
        """
        trade_dict = trade.to_dict()
        new_row = pd.DataFrame([trade_dict])
        self._portfolio = pd.concat([self._portfolio, new_row], ignore_index=True)
        self._dirty = True
        return len(self._portfolio) - 1

    def update_trade(self, index: int, updates: Dict[str, Any]) -> None:
        """Update trade at given index."""
        for key, value in updates.items():
            if key in self._portfolio.columns:
                self._portfolio.loc[index, key] = value
        self._dirty = True

    def get_open_trades(self) -> pd.DataFrame:
        """Get all open trades."""
        if 'isOpen' in self._portfolio.columns:
            return self._portfolio[self._portfolio['isOpen'] == 1]
        return self._portfolio

    def get_trades_by_symbol(self, symbol: str) -> pd.DataFrame:
        """Get all trades for a symbol."""
        return self._portfolio[
            self._portfolio['Symbol'].str.match(f"{symbol}$", na=False)
        ]

    def get_trades_by_trader(self, trader: str) -> pd.DataFrame:
        """Get all trades by a specific trader."""
        return self._portfolio[self._portfolio['Trader'] == trader]

    def find_open_trade(
        self,
        symbol: str,
        trader: str,
        trade_type: str
    ) -> Optional[int]:
        """Find open trade matching criteria."""
        mask = (
            (self._portfolio['isOpen'] == 1) &
            (self._portfolio['Symbol'].str.match(f"{symbol}$", na=False)) &
            (self._portfolio['Trader'] == trader) &
            (self._portfolio['Type'] == trade_type)
        )
        matches = self._portfolio[mask]
        if len(matches) == 1:
            return matches.index[0]
        elif len(matches) > 1:
            return matches.index[-1]
        return None

    def close_trade(self, index: int, pnl: Optional[float] = None) -> None:
        """Close trade at given index."""
        self._portfolio.loc[index, 'isOpen'] = 0
        if pnl is not None:
            self._portfolio.loc[index, 'PnL$'] = pnl
        self._dirty = True

    def get_stats(self) -> PortfolioStats:
        """Calculate portfolio statistics."""
        return PortfolioStats().calculate(self._portfolio)

    def get_trader_stats(self) -> pd.DataFrame:
        """Calculate stats grouped by trader."""
        if self._portfolio.empty:
            return pd.DataFrame()

        grouped = self._portfolio.groupby('Trader').agg({
            'Symbol': 'count',
            'isOpen': 'sum',
            'PnL$': ['sum', 'mean', 'count']
        })

        grouped.columns = ['Total', 'Open', 'PnL_sum', 'PnL_mean', 'Trades']
        grouped['WinRate'] = grouped.apply(
            lambda x: len(self._portfolio[
                (self._portfolio['Trader'] == x.name) &
                (self._portfolio['isOpen'] == 0) &
                (self._portfolio['PnL$'] > 0)
            ]) / max(1, len(self._portfolio[
                (self._portfolio['Trader'] == x.name) &
                (self._portfolio['isOpen'] == 0)
            ])) * 100,
            axis=1
        )

        return grouped.reset_index()
