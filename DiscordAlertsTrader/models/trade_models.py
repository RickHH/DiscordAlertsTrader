#!/usr/bin/env python3
"""
Data models for the Discord Alerts Trader application.
Uses dataclasses for type safety and better code organization.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any, Union
from enum import Enum
import json


class TradeAction(Enum):
    BTO = "BTO"  # Buy To Open (Long)
    STC = "STC"  # Sell To Close (Long)
    STO = "STO"  # Sell To Open (Short)
    BTC = "BTC"  # Buy To Close (Short)
    EXIT_UPDATE = "ExitUpdate"
    AVG = "Avg"


class AssetType(Enum):
    STOCK = "stock"
    OPTION = "option"


class OrderStatus(Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    WORKING = "WORKING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    EXECUTED = "EXECUTED"
    PARTIAL = "PARTIAL"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very high"
    LOTTO = "lotto"
    YOLO = "yolo"


@dataclass
class ExitTarget:
    price: Optional[float] = None
    percentage: Optional[float] = None
    trailing_stop: Optional[float] = None
    is_triggered: bool = False

    @classmethod
    def from_string(cls, value: str) -> 'ExitTarget':
        if not value:
            return cls()
        if isinstance(value, (int, float)):
            return cls(price=float(value))

        trailing_stop = None
        percentage = None
        price = None

        if 'TS' in str(value).upper():
            parts = str(value).upper().split('TS')
            try:
                first = parts[0].replace('%', '')
                if first:
                    if '%' in str(value):
                        percentage = float(first)
                    else:
                        price = float(first)
                if len(parts) > 1 and parts[1]:
                    trailing_stop = float(parts[1].replace('%', ''))
            except ValueError:
                pass
        elif '%' in str(value):
            percentage = float(str(value).replace('%', ''))
        else:
            try:
                price = float(value)
            except ValueError:
                pass

        return cls(price=price, percentage=percentage, trailing_stop=trailing_stop)

    def to_price(self, reference_price: float, is_short: bool = False) -> Optional[float]:
        if self.price is not None:
            return self.price
        if self.percentage is not None:
            if is_short:
                return reference_price * (1 - self.percentage / 100)
            return reference_price * (1 + self.percentage / 100)
        return None


@dataclass
class ExitPlan:
    pt1: Optional[ExitTarget] = None
    pt2: Optional[ExitTarget] = None
    pt3: Optional[ExitTarget] = None
    sl: Optional[ExitTarget] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ExitPlan':
        def parse_exit(value: Any) -> Optional[ExitTarget]:
            if value is None:
                return None
            if isinstance(value, ExitTarget):
                return value
            return ExitTarget.from_string(str(value))

        return cls(
            pt1=parse_exit(data.get('PT1')),
            pt2=parse_exit(data.get('PT2')),
            pt3=parse_exit(data.get('PT3')),
            sl=parse_exit(data.get('SL'))
        )

    @classmethod
    def from_string(cls, plan_str: str) -> 'ExitPlan':
        if not plan_str or plan_str in ('{}', 'None', ''):
            return cls()
        try:
            data = json.loads(plan_str.replace("'", '"'))
            return cls.from_dict(data)
        except (json.JSONDecodeError, TypeError):
            return cls()

    def to_dict(self) -> Dict[str, Any]:
        result = {}
        for key, value in [('PT1', self.pt1), ('PT2', self.pt2),
                           ('PT3', self.pt3), ('SL', self.sl)]:
            if value is not None:
                if value.price is not None:
                    result[key] = value.price
                elif value.percentage is not None:
                    result[key] = f"{value.percentage}%"
                    if value.trailing_stop is not None:
                        result[key] += f"TS{value.trailing_stop}%"
        return result

    def __str__(self) -> str:
        return json.dumps(self.to_dict())


@dataclass
class Order:
    action: TradeAction
    symbol: str
    price: float
    quantity: Optional[int] = None
    asset: AssetType = AssetType.STOCK
    risk: Optional[RiskLevel] = None
    exit_plan: Optional[ExitPlan] = None
    trader: Optional[str] = None
    date: Optional[datetime] = None
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    trailing_stop: Optional[float] = None
    avg_price: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Order':
        action_str = data.get('action', 'BTO').upper()
        try:
            action = TradeAction(action_str)
        except ValueError:
            action = TradeAction.BTO

        asset_str = data.get('asset', 'stock').lower()
        try:
            asset = AssetType(asset_str)
        except ValueError:
            asset = AssetType.STOCK

        risk = None
        if data.get('risk'):
            try:
                risk = RiskLevel(data['risk'].lower())
            except ValueError:
                pass

        exit_plan = None
        if any(data.get(f'PT{i}') or data.get('SL') for i in range(1, 4)):
            exit_plan = ExitPlan.from_dict({
                'PT1': data.get('PT1'),
                'PT2': data.get('PT2'),
                'PT3': data.get('PT3'),
                'SL': data.get('SL')
            })

        return cls(
            action=action,
            symbol=data.get('symbol', data.get('Symbol', '')),
            price=float(data.get('price', 0)),
            quantity=data.get('Qty'),
            asset=asset,
            risk=risk,
            exit_plan=exit_plan,
            trader=data.get('Trader'),
            date=datetime.now(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'action': self.action.value,
            'symbol': self.symbol,
            'price': self.price,
            'quantity': self.quantity,
            'asset': self.asset.value,
            'risk': self.risk.value if self.risk else None,
            'trader': self.trader,
        }


@dataclass
class Trade:
    date: datetime
    symbol: str
    action: TradeAction
    quantity: int
    price: float
    is_open: bool = True
    asset: AssetType = AssetType.STOCK
    trader: Optional[str] = None
    channel: Optional[str] = None
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    exit_plan: Optional[ExitPlan] = None
    pnl: Optional[float] = None
    pnl_dollar: Optional[float] = None
    price_alert: Optional[float] = None
    price_actual: Optional[float] = None
    filled_quantity: Optional[int] = None
    risk: Optional[RiskLevel] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'Date': self.date.strftime('%Y-%m-%d %H:%M:%S.%f') if self.date else None,
            'Symbol': self.symbol,
            'Type': self.action.value,
            'Qty': self.quantity,
            'Price': self.price,
            'isOpen': 1 if self.is_open else 0,
            'Asset': self.asset.value,
            'Trader': self.trader,
            'Channel': self.channel,
            'ordID': self.order_id,
            'BTO-Status': self.status.value if self.status else None,
            'exit_plan': str(self.exit_plan) if self.exit_plan else None,
            'PnL': self.pnl,
            'PnL$': self.pnl_dollar,
            'Price-alert': self.price_alert,
            'Price-actual': self.price_actual,
            'filledQty': self.filled_quantity,
            'Risk': self.risk.value if self.risk else None,
        }


@dataclass
class AlertMessage:
    author: str
    content: str
    channel: Optional[str] = None
    date: datetime = field(default_factory=datetime.now)
    author_id: Optional[int] = None
    parsed: Optional[str] = None
    order: Optional[Order] = None

    @classmethod
    def from_series(cls, series: 'pd.Series') -> 'AlertMessage':
        return cls(
            author=series.get('Author', ''),
            content=series.get('Content', ''),
            channel=series.get('Channel'),
            date=datetime.strptime(series['Date'], '%Y-%m-%d %H:%M:%S.%f') if 'Date' in series else datetime.now(),
            author_id=series.get('AuthorID'),
        )
