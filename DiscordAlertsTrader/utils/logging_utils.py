#!/usr/bin/env python3
"""
Unified logging and error handling utilities.
Provides consistent logging across the application.
"""
import logging
import traceback
import queue
from datetime import datetime
from typing import Optional, Callable, Any, TypeVar, Union
from functools import wraps
from enum import Enum

from colorama import Fore, Back, Style, init

init(autoreset=True)


class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


COLOR_MAP = {
    LogLevel.DEBUG: Fore.WHITE,
    LogLevel.INFO: Fore.GREEN,
    LogLevel.WARNING: Fore.YELLOW,
    LogLevel.ERROR: Fore.RED,
    LogLevel.CRITICAL: Fore.RED + Back.WHITE,
}


class ColoredFormatter(logging.Formatter):
    def format(self, record):
        if record.levelno >= logging.ERROR:
            color = Fore.RED
        elif record.levelno >= logging.WARNING:
            color = Fore.YELLOW
        elif record.levelno >= logging.INFO:
            color = Fore.GREEN
        else:
            color = Fore.WHITE

        record.levelname = f"{color}{record.levelname}{Style.RESET_ALL}"
        return super().format(record)


def setup_logger(
    name: str,
    level: int = logging.INFO,
    log_file: Optional[str] = None
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(ColoredFormatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    ))
    logger.addHandler(console_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


TRADER_LOGGER = setup_logger('DiscordAlertsTrader')
BROKERAGE_LOGGER = setup_logger('DiscordAlertsTrader.brokerage')
GUI_LOGGER = setup_logger('DiscordAlertsTrader.gui')


def log_exception(logger: logging.Logger, exc: Exception, context: str = ""):
    exc_traceback = ''.join(traceback.format_tb(exc.__traceback__))
    logger.error(
        f"{context}\n"
        f"Exception: {type(exc).__name__}: {exc}\n"
        f"Traceback:\n{exc_traceback}"
    )


def print_colored(message: str, level: LogLevel = LogLevel.INFO):
    color = COLOR_MAP.get(level, Fore.WHITE)
    print(f"{color}{message}{Style.RESET_ALL}")


def print_success(message: str):
    print(f"{Fore.GREEN}{message}{Style.RESET_ALL}")


def print_error(message: str):
    print(f"{Fore.RED}{message}{Style.RESET_ALL}")


def print_warning(message: str):
    print(f"{Fore.YELLOW}{message}{Style.RESET_ALL}")


def print_info(message: str):
    print(f"{Fore.CYAN}{message}{Style.RESET_ALL}")


T = TypeVar('T')


def retry_on_exception(
    max_retries: int = 3,
    delay_seconds: float = 1.0,
    logger: Optional[logging.Logger] = None,
    context: str = ""
) -> Callable[[Callable[..., T]], Callable[..., Optional[T]]]:
    def decorator(func: Callable[..., T]) -> Callable[..., Optional[T]]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Optional[T]:
            last_exception = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if logger:
                        log_exception(
                            logger,
                            e,
                            f"{context} - Attempt {attempt}/{max_retries}"
                        )
                    if attempt < max_retries:
                        import time
                        time.sleep(delay_seconds)

            if logger:
                logger.error(
                    f"{context} - All {max_retries} attempts failed. "
                    f"Last error: {last_exception}"
                )
            return None
        return wrapper
    return decorator


class QueueLogger:
    """Logger that outputs to a queue for GUI integration."""

    def __init__(self, maxsize: int = 100):
        self.queue: queue.Queue = queue.Queue(maxsize=maxsize)

    def put(
        self,
        message: str,
        level: LogLevel = LogLevel.INFO,
        color: Optional[str] = None,
        background: Optional[str] = None
    ):
        entry = {
            'message': message,
            'level': level.value,
            'color': color,
            'background': background,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        try:
            self.queue.put_nowait(entry)
        except queue.Full:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(entry)
            except queue.Empty:
                pass

    def info(self, message: str, color: str = "green"):
        self.put(message, LogLevel.INFO, color)

    def warning(self, message: str, color: str = "yellow"):
        self.put(message, LogLevel.WARNING, color)

    def error(self, message: str, color: str = "red"):
        self.put(message, LogLevel.ERROR, color)

    def debug(self, message: str):
        self.put(message, LogLevel.DEBUG, "white")


class ErrorHandler:
    """Centralized error handling with recovery strategies."""

    @staticmethod
    def handle_brokerage_error(
        error: Exception,
        operation: str,
        logger: logging.Logger = BROKERAGE_LOGGER
    ) -> bool:
        error_msg = f"Brokerage error during {operation}: {type(error).__name__}: {error}"
        logger.error(error_msg)

        if "timeout" in str(error).lower():
            logger.warning("Timeout detected, retry may help")
            return False
        elif "auth" in str(error).lower() or "token" in str(error).lower():
            logger.critical("Authentication error - check credentials")
            return True
        elif "rate limit" in str(error).lower():
            logger.warning("Rate limited - will retry after cooldown")
            return False

        return False

    @staticmethod
    def handle_order_error(
        error: Exception,
        order: dict,
        logger: logging.Logger = TRADER_LOGGER
    ) -> bool:
        error_msg = (
            f"Order error for {order.get('action', 'UNKNOWN')} "
            f"{order.get('symbol', order.get('Symbol', 'UNKNOWN'))}: "
            f"{type(error).__name__}: {error}"
        )
        logger.error(error_msg)

        if "insufficient" in str(error).lower():
            logger.warning("Insufficient funds or buying power")
            return True
        elif "rejected" in str(error).lower():
            logger.warning("Order rejected by brokerage")
            return True

        return False


def with_error_handling(
    logger: Optional[logging.Logger] = None,
    context: str = "",
    return_on_error: Any = None
):
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if logger:
                    log_exception(logger, e, context)
                else:
                    log_exception(TRADER_LOGGER, e, context or func.__name__)
                return return_on_error
        return wrapper
    return decorator
