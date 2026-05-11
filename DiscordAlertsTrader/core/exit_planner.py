#!/usr/bin/env python3
"""
Exit planning module for AlertsTrader.
Handles profit targets, stop losses, and trailing stops.
"""
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class ExitTarget:
    """Represents a single exit target."""
    price: Optional[float] = None
    percentage: Optional[float] = None
    trailing_stop: Optional[float] = None
    quantity_pct: float = 1.0
    is_triggered: bool = False

    def to_dict(self) -> Dict[str, Any]:
        result = {}
        if self.price is not None:
            result['price'] = self.price
        if self.percentage is not None:
            result['percentage'] = self.percentage
        if self.trailing_stop is not None:
            result['trailing_stop'] = self.trailing_stop
        if self.quantity_pct != 1.0:
            result['quantity_pct'] = self.quantity_pct
        return result


@dataclass
class ExitPlan:
    """Complete exit plan with multiple targets and stop loss."""
    pt1: Optional[ExitTarget] = None
    pt2: Optional[ExitTarget] = None
    pt3: Optional[ExitTarget] = None
    sl: Optional[ExitTarget] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ExitPlan':
        """Create ExitPlan from dictionary."""
        def parse_target(key: str) -> Optional[ExitTarget]:
            if key not in data or data[key] is None:
                return None

            value = data[key]
            if isinstance(value, (int, float)):
                return ExitTarget(price=float(value))

            if isinstance(value, str):
                trailing_stop = None
                percentage = None
                price = None

                if 'TS' in value.upper():
                    parts = value.upper().split('TS')
                    if '%' in parts[0]:
                        percentage = float(parts[0].replace('%', ''))
                    else:
                        price = float(parts[0]) if parts[0] else None
                    if len(parts) > 1 and parts[1]:
                        trailing_stop = float(parts[1].replace('%', ''))
                elif '%' in value:
                    percentage = float(value.replace('%', ''))
                else:
                    try:
                        price = float(value)
                    except ValueError:
                        return None

                return ExitTarget(
                    price=price,
                    percentage=percentage,
                    trailing_stop=trailing_stop
                )

            return None

        return cls(
            pt1=parse_target('PT1'),
            pt2=parse_target('PT2'),
            pt3=parse_target('PT3'),
            sl=parse_target('SL')
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        result = {}
        for key, target in [('PT1', self.pt1), ('PT2', self.pt2),
                           ('PT3', self.pt3), ('SL', self.sl)]:
            if target is not None:
                result[key] = target.to_dict()
        return result

    def to_string(self) -> str:
        """Convert to string representation for storage."""
        parts = []
        for key, target in [('PT1', self.pt1), ('PT2', self.pt2),
                           ('PT3', self.pt3), ('SL', self.sl)]:
            if target is not None:
                if target.percentage is not None:
                    val = f"{target.percentage}%"
                    if target.trailing_stop is not None:
                        val += f"TS{target.trailing_stop}%"
                    parts.append(f"'{key}': '{val}'")
                elif target.price is not None:
                    parts.append(f"'{key}': {target.price}")
        return '{' + ', '.join(parts) + '}'

    def is_empty(self) -> bool:
        """Check if exit plan has no targets."""
        return all(t is None for t in [self.pt1, self.pt2, self.pt3, self.sl])

    def get_targets(self) -> List[ExitTarget]:
        """Get all non-None targets in order."""
        return [t for t in [self.pt1, self.pt2, self.pt3] if t is not None]


class ExitPlanner:
    """
    Manages exit planning for trades.

    Responsibilities:
    - Parse exit plans from alerts
    - Convert percentage exits to price-based
    - Manage trailing stops
    - Calculate exit order quantities
    """

    def __init__(self, max_targets: int = 4, default_plan: Optional[Dict] = None):
        self.max_targets = max_targets
        self.default_plan = ExitPlan.from_dict(default_plan or {})

    def parse_exit_plan(self, order: Dict[str, Any]) -> ExitPlan:
        """
        Parse exit plan from order dictionary.

        Args:
            order: Order with optional PT1, PT2, PT3, SL keys

        Returns:
            ExitPlan object
        """
        data = {}
        for key in ['PT1', 'PT2', 'PT3', 'SL']:
            if key in order and order[key] is not None:
                data[key] = order[key]

        if data:
            return ExitPlan.from_dict(data)
        return ExitPlan()

    def get_default_plan(self) -> ExitPlan:
        """Get the default exit plan."""
        return self.default_plan

    def merge_with_defaults(self, plan: ExitPlan) -> ExitPlan:
        """Merge parsed plan with defaults."""
        if plan.is_empty():
            return self.default_plan

        result = ExitPlan()
        for key in ['pt1', 'pt2', 'pt3', 'sl']:
            plan_target = getattr(plan, key)
            default_target = getattr(self.default_plan, key)
            setattr(result, key, plan_target if plan_target is not None else default_target)

        return result

    def calculate_price_targets(
        self,
        entry_price: float,
        plan: ExitPlan,
        is_short: bool = False
    ) -> Dict[str, float]:
        """
        Convert percentage-based exits to price targets.

        Args:
            entry_price: Entry price for the position
            plan: Exit plan with percentages or prices
            is_short: True if short position

        Returns:
            Dictionary mapping 'PT1', 'PT2', etc. to calculated prices
        """
        targets = {}

        for key, target in [('PT1', plan.pt1), ('PT2', plan.pt2),
                           ('PT3', plan.pt3), ('SL', plan.sl)]:
            if target is None:
                continue

            if target.percentage is not None:
                if is_short:
                    price = entry_price * (1 - target.percentage / 100)
                else:
                    price = entry_price * (1 + target.percentage / 100)
                targets[key] = round(price, 2)
            elif target.price is not None:
                targets[key] = target.price

        return targets

    def parse_trailing_stop(
        self,
        ts_str: str,
        current_price: float
    ) -> tuple[float, float]:
        """
        Parse trailing stop string to target and offset values.

        Args:
            ts_str: Trailing stop string (e.g., "50%TS5%" or "130TS2")
            current_price: Current price for percentage calculations

        Returns:
            Tuple of (target_price, trailing_stop_offset)
        """
        if not ts_str or 'TS' not in ts_str.upper():
            return 0, 0

        parts = ts_str.upper().split('TS')

        if '%' in parts[0]:
            target_pct = float(parts[0].replace('%', ''))
            target_price = current_price * (1 + target_pct / 100)
        else:
            target_price = float(parts[0]) if parts[0] else current_price

        if len(parts) > 1 and parts[1]:
            if '%' in parts[1]:
                ts_offset = float(parts[1].replace('%', ''))
                ts_offset = current_price * (ts_offset / 100)
            else:
                ts_offset = float(parts[1])
        else:
            ts_offset = 0

        return round(target_price, 2), round(ts_offset, 2)

    def calculate_exit_quantities(
        self,
        total_qty: int,
        num_targets: int
    ) -> List[int]:
        """
        Calculate exit quantities for multiple profit targets.

        Args:
            total_qty: Total quantity to exit
            num_targets: Number of profit targets

        Returns:
            List of quantities for each target
        """
        if num_targets <= 0:
            return []

        base_qty = total_qty / num_targets
        quantities = [int(base_qty) for _ in range(num_targets)]
        quantities[-1] += total_qty - sum(quantities)
        return quantities

    def should_trigger_trailing_stop(
        self,
        current_price: float,
        target_price: float,
        trailing_stop_offset: float,
        is_short: bool = False
    ) -> bool:
        """
        Check if trailing stop should trigger.

        Args:
            current_price: Current market price
            target_price: Profit target price
            trailing_stop_offset: Trailing stop offset
            is_short: True if short position

        Returns:
            True if trailing stop should trigger
        """
        if trailing_stop_offset <= 0:
            return False

        if is_short:
            return current_price >= (target_price - trailing_stop_offset)
        else:
            return current_price <= (target_price - trailing_stop_offset)

    def format_exit_order(
        self,
        symbol: str,
        exit_plan: ExitPlan,
        trade_type: str,
        qty: int
    ) -> Dict[str, Any]:
        """
        Format exit order parameters.

        Args:
            symbol: Trading symbol
            exit_plan: Exit plan with targets
            trade_type: "STC" or "BTC"
            qty: Total quantity to exit

        Returns:
            Dictionary with exit order parameters
        """
        order = {
            'symbol': symbol,
            'action': trade_type,
            'Qty': qty
        }

        quantities = self.calculate_exit_quantities(qty, len(exit_plan.get_targets()))
        for i, (key, target) in enumerate([('PT1', exit_plan.pt1), ('PT2', exit_plan.pt2),
                                           ('PT3', exit_plan.pt3)]):
            if target is not None and i < len(quantities):
                order[f'{key}_Qty'] = quantities[i]
                if target.trailing_stop is not None:
                    order[f'{key}_TS'] = target.trailing_stop

        if exit_plan.sl is not None:
            order['SL_Qty'] = qty
            if exit_plan.sl.trailing_stop is not None:
                order['SL_TS'] = exit_plan.sl.trailing_stop

        return order
