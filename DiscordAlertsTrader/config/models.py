#!/usr/bin/env python3
"""
Pydantic models for configuration validation.
Provides type-safe configuration access with validation.
"""
from typing import Optional, Dict, Any, List, Union
from pydantic import BaseModel, Field, field_validator
from datetime import time


class DiscordConfig(BaseModel):
    """Discord configuration settings."""
    discord_token: str = Field(default="", description="Discord authentication token")
    authors_subscribed: str = Field(default="", description="Comma-separated subscribed authors")
    channel_IDS: str = Field(default="{}", description="Channel ID mappings")
    notify_alerts_to_discord: bool = Field(default=False, description="Send alerts to Discord")
    channel_to_notify: Optional[str] = Field(default=None, description="Channel to notify")

    @field_validator('discord_token')
    @classmethod
    def validate_token(cls, v: str) -> str:
        if v and len(v) < 50:
            raise ValueError("Discord token appears too short")
        return v


class GeneralConfig(BaseModel):
    """General application settings."""
    data_dir: str = Field(default="./data", description="Data directory path")
    DO_BTO_TRADES: bool = Field(default=False, description="Enable buy to open trades")
    DO_STO_TRADES: bool = Field(default=False, description="Enable short trades")
    off_hours: str = Field(default="16:00", description="End of trading hours")
    brokerage: str = Field(default="", description="Brokerage name")

    @property
    def data_dir_path(self) -> str:
        return self.data_dir


class OrderConfig(BaseModel):
    """Order configuration settings."""
    default_exits: str = Field(default="{}", description="Default exit plan")
    max_price_diff: str = Field(default="{}", description="Max price difference by asset")
    auto_trade: bool = Field(default=False, description="Enable automatic trading")
    sell_current_price: bool = Field(default=False, description="Use current price for sells")
    exclude_tickers: str = Field(default="", description="Excluded ticker symbols")
    max_trade_capital: str = Field(default="{}", description="Max trade capital per trader")
    default_bto_qty: str = Field(default="{}", description="Default BTO quantity per trader")
    trade_capital: str = Field(default="{}", description="Trade capital per trader")
    kill_if_nofill: int = Field(default=300, description="Kill order if not filled in seconds")

    @field_validator('default_exits', 'max_price_diff', 'max_trade_capital',
                     'default_bto_qty', 'trade_capital')
    @classmethod
    def validate_json_str(cls, v: str) -> str:
        if v and v not in ('{}', ''):
            import json
            try:
                json.loads(v)
            except json.JSONDecodeError:
                raise ValueError(f"Invalid JSON format: {v}")
        return v


class ShortingConfig(BaseModel):
    """Short selling configuration."""
    STO_price: str = Field(default="ask", description="Price type for STO")
    STO_trailingstop: str = Field(default="", description="Trailing stop percentage")
    max_price_diff: str = Field(default="5", description="Max price difference")
    max_strike: str = Field(default="400", description="Maximum strike price")
    max_dte: str = Field(default="30", description="Maximum DTE")
    min_price: str = Field(default="0.30", description="Minimum premium price")
    default_sto_qty: str = Field(default="buy_one", description="Default STO quantity method")
    margin_capital: str = Field(default="20000", description="Margin capital")
    trade_capital: str = Field(default="300", description="Trade capital")
    max_trade_capital: str = Field(default="5000", description="Maximum trade capital")
    min_trade_capital: str = Field(default="100", description="Minimum trade capital")
    ignore_alert_qty: bool = Field(default=False, description="Ignore alert quantity")
    BTC_PT: str = Field(default="", description="BTC profit target percentage")
    BTC_SL: str = Field(default="", description="BTC stop loss percentage")
    BTC_EOD: bool = Field(default=False, description="Close positions end of day")
    BTC_EOD_PT_SL: str = Field(default="", description="EOD PT and SL values")
    avg_down: Optional[str] = Field(default=None, description="Average down configuration")


class PortfolioNamesConfig(BaseModel):
    """Portfolio file names configuration."""
    portfolio_fname: str = Field(default="portfolio.csv", description="Main portfolio file")
    alerts_log_fname: str = Field(default="alerts_log.csv", description="Alerts log file")
    tracker_portfolio_name: str = Field(default="tracker_portfolio.csv",
                                         description="Tracker portfolio file")


class ColumnNamesConfig(BaseModel):
    """Column names configuration for CSV files."""
    portfolio: str = Field(default="", description="Portfolio columns")
    alerts_log: str = Field(default="", description="Alerts log columns")
    tracker_portfolio: str = Field(default="", description="Tracker columns")
    chan_hist: str = Field(default="", description="Channel history columns")


class AppConfig(BaseModel):
    """Main application configuration."""
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    order_configs: OrderConfig = Field(default_factory=OrderConfig)
    shorting: ShortingConfig = Field(default_factory=ShortingConfig)
    portfolio_names: PortfolioNamesConfig = Field(default_factory=PortfolioNamesConfig)
    col_names: ColumnNamesConfig = Field(default_factory=ColumnNamesConfig)

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Dict]) -> 'AppConfig':
        """Create config from flat dictionary structure."""
        return cls(
            discord=DiscordConfig(**config_dict.get('discord', {})),
            general=GeneralConfig(**config_dict.get('general', {})),
            order_configs=OrderConfig(**config_dict.get('order_configs', {})),
            shorting=ShortingConfig(**config_dict.get('shorting', {})),
            portfolio_names=PortfolioNamesConfig(**config_dict.get('portfolio_names', {})),
            col_names=ColumnNamesConfig(**config_dict.get('col_names', {}))
        )


class ConfigManager:
    """Manages application configuration with caching and validation."""

    _instance: Optional['ConfigManager'] = None
    _config: Optional[AppConfig] = None
    _raw_config: Optional[Dict] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load_from_file(self, config_path: str) -> None:
        """Load configuration from INI file."""
        import configparser
        import os

        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")

        parser = configparser.ConfigParser()
        parser.read(config_path)

        self._raw_config = {section: dict(parser.items(section))
                           for section in parser.sections()}

        try:
            self._config = AppConfig.from_dict(self._raw_config)
        except Exception as e:
            import warnings
            warnings.warn(f"Config validation failed: {e}. Using raw config.")
            self._config = None

    def get(self, section: str, key: Optional[str] = None,
            default: Any = None) -> Any:
        """Get configuration value."""
        if self._raw_config is None:
            return default

        if key is None:
            return self._raw_config.get(section, {})

        section_config = self._raw_config.get(section, {})
        value = section_config.get(key, default)

        parser = self._raw_config.get(section, {}).get(f'_parser_{key}')
        if parser == 'boolean':
            return value.lower() in ('true', 'yes', '1', 'on')
        return value

    @property
    def validated(self) -> Optional[AppConfig]:
        """Get validated configuration if available."""
        return self._config

    @property
    def raw(self) -> Optional[Dict]:
        """Get raw configuration dictionary."""
        return self._raw_config


def create_config_validator():
    """Factory function for creating a validated config manager."""
    return ConfigManager()
