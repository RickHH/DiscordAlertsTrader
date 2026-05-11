import json
import re
import logging
from typing import Any, Dict, Optional, Union
from datetime import datetime

logger = logging.getLogger(__name__)


def safe_eval_expr(expr: str) -> Union[int, float]:
    expr = str(expr).strip()
    if not expr or expr == "None" or expr == "":
        return None
    try:
        if "%" in expr:
            return expr
        if "TS" in expr:
            return expr
        return eval(expr)
    except (ValueError, SyntaxError) as e:
        logger.warning(f"Failed to eval expression '{expr}': {e}")
        return expr


def safe_eval_dict(config_str: str) -> Dict[str, Any]:
    if not config_str or config_str.strip() == "":
        return {}
    config_str = config_str.strip()
    if config_str.startswith("{") and config_str.endswith("}"):
        try:
            return json.loads(config_str)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse failed for '{config_str}': {e}")
    try:
        result = eval(config_str, {"__builtins__": {}}, {})
        if isinstance(result, dict):
            return result
        return {}
    except Exception as e:
        logger.warning(f"safe_eval_dict failed for '{config_str}': {e}")
        return {}


def safe_eval_list(config_str: str) -> list:
    if not config_str or config_str.strip() in ["", "[]"]:
        return []
    try:
        result = json.loads(config_str)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        result = eval(config_str, {"__builtins__": {}}, {})
        if isinstance(result, list):
            return result
        return []
    except Exception as e:
        logger.warning(f"safe_eval_list failed for '{config_str}': {e}")
        return []


def parse_exit_plan(exit_plan_str: str) -> Dict[str, Any]:
    if not exit_plan_str or exit_plan_str.strip() in ["", "None", "{}"]:
        return {"PT1": None, "PT2": None, "PT3": None, "SL": None}
    try:
        result = json.loads(exit_plan_str)
        if isinstance(result, dict):
            for key in ["PT1", "PT2", "PT3", "SL"]:
                if key not in result:
                    result[key] = None
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        result = eval(exit_plan_str, {"__builtins__": {}}, {})
        if isinstance(result, dict):
            return result
    except Exception as e:
        logger.warning(f"parse_exit_plan failed for '{exit_plan_str}': {e}")
    return {"PT1": None, "PT2": None, "PT3": None, "SL": None}


def parse_avg_down(avg_down_str: str) -> Dict[str, Any]:
    if not avg_down_str or avg_down_str.strip() in ["", "None", "{}"]:
        return {}
    try:
        result = json.loads(avg_down_str)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        result = eval(avg_down_str, {"__builtins__": {}}, {})
        if isinstance(result, dict):
            return result
    except Exception as e:
        logger.warning(f"parse_avg_down failed for '{avg_down_str}': {e}")
    return {}


def parse_trailingstop(ts_str: str) -> Optional[Dict[str, Any]]:
    if not ts_str or ts_str.strip() == "" or ts_str == "nan":
        return None
    try:
        parts = ts_str.split(",")
        if len(parts) == 2:
            ts_key = parts[0].strip()
            ts_val = parts[1].strip()
            if ts_key.startswith("ts:"):
                ts_const = float(ts_key.split(":")[1])
            else:
                return None
            if ts_val.startswith("max_price:"):
                max_price = float(ts_val.split(":")[1])
            else:
                return None
            return {"ts_const": ts_const, "max_price": max_price}
    except (ValueError, IndexError) as e:
        logger.warning(f"parse_trailingstop failed for '{ts_str}': {e}")
    return None


def parse_prices(prices_str: str) -> Optional[float]:
    if not prices_str or prices_str.strip() in ["", "None"]:
        return None
    try:
        if "/" in prices_str:
            values = [float(x) for x in prices_str.replace(",", "/").split("/") if x.strip()]
            return sum(values) / len(values)
        return float(prices_str)
    except (ValueError, TypeError, ZeroDivisionError) as e:
        logger.warning(f"parse_prices failed for '{prices_str}': {e}")
        return None


def parse_number_list(config_str: str) -> list:
    if not config_str or config_str.strip() in ["", "[]"]:
        return []
    if config_str.strip().startswith("["):
        try:
            result = json.loads(config_str)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass
    try:
        result = eval(config_str, {"__builtins__": {}}, {})
        if isinstance(result, list):
            return result
    except Exception:
        pass
    return []


def safe_parse_float(value: Any) -> Optional[float]:
    if value is None or (isinstance(value, str) and value.strip() in ["", "None", "nan"]):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def safe_parse_int(value: Any) -> Optional[int]:
    if value is None or (isinstance(value, str) and value.strip() in ["", "None", "nan"]):
        return None
    try:
        return int(float(str(value)))
    except (ValueError, TypeError):
        return None
