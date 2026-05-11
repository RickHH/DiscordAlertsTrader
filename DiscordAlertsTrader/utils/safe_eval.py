#!/usr/bin/env python3
"""
Safe evaluation utilities to replace eval() for security.
Only allows basic math operations, no arbitrary code execution.
"""
import ast
import json
import operator
from typing import Any, Union, Dict, List, Optional
from dataclasses import dataclass, field


SAFE_MATH_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

SAFE_BUILTINS = {
    'abs': abs,
    'round': round,
    'min': min,
    'max': max,
    'sum': sum,
    'float': float,
    'int': int,
    'str': str,
    'bool': bool,
    'len': len,
    'range': range,
}


class SafeEvalError(Exception):
    pass


def safe_eval(expr: str) -> Union[int, float]:
    """
    Safely evaluate mathematical expressions.
    Only allows basic math operations - no function calls, no imports.
    """
    try:
        node = ast.parse(expr.strip(), mode='eval')
        return _eval_node(node.body)
    except (SyntaxError, ValueError, TypeError, KeyError) as e:
        raise SafeEvalError(f"Invalid expression: {expr}") from e


def _eval_node(node: ast.AST) -> Union[int, float]:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise SafeEvalError(f"Unsupported constant type: {type(node.value)}")

    elif isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        op_type = type(node.op)
        if op_type not in SAFE_MATH_OPERATORS:
            raise SafeEvalError(f"Unsupported operator: {op_type.__name__}")
        return SAFE_MATH_OPERATORS[op_type](left, right)

    elif isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        op_type = type(node.op)
        if op_type not in SAFE_MATH_OPERATORS:
            raise SafeEvalError(f"Unsupported unary operator: {op_type.__name__}")
        return SAFE_MATH_OPERATORS[op_type](operand)

    elif isinstance(node, ast.Name):
        raise SafeEvalError(f"Variable access not allowed: {node.id}")

    elif isinstance(node, ast.Call):
        raise SafeEvalError("Function calls not allowed in safe_eval")

    else:
        raise SafeEvalError(f"Unsupported AST node: {type(node).__name__}")


def safe_json_eval(json_str: str) -> Any:
    """
    Safely parse JSON that may contain simple expressions.
    Handles formats like: {"PT1": "50%", "SL": "30%"}
    """
    if not json_str or json_str.strip() in ('', 'None'):
        return None

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return json_str


@dataclass
class ExitPlan:
    PT1: Optional[Union[float, str]] = None
    PT2: Optional[Union[float, str]] = None
    PT3: Optional[Union[float, str]] = None
    SL: Optional[Union[float, str]] = None

    @classmethod
    def from_string(cls, plan_str: str) -> 'ExitPlan':
        if not plan_str or plan_str == '{}' or plan_str == 'None':
            return cls()
        try:
            data = json.loads(plan_str.replace("'", '"'))
            return cls(**{k: v for k, v in data.items() if k in ['PT1', 'PT2', 'PT3', 'SL']})
        except json.JSONDecodeError:
            return cls()

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    def __str__(self) -> str:
        return json.dumps(self.to_dict())


@dataclass
class ConfigValues:
    """Safe config value parser with validation."""

    @staticmethod
    def parse_number(value: str) -> Union[int, float, None]:
        if not value or value.strip() == '':
            return None
        try:
            if '.' in value:
                return float(value)
            return int(value)
        except ValueError:
            return None

    @staticmethod
    def parse_percentage(value: str) -> Optional[float]:
        if not value or '%' not in value:
            return None
        try:
            return float(value.replace('%', ''))
        except ValueError:
            return None

    @staticmethod
    def parse_dict(value: str) -> Dict[str, Any]:
        if not value:
            return {}
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def parse_trailing_stop(value: str) -> Optional[Dict[str, float]]:
        if not value or 'TS' not in value.upper():
            return None
        try:
            parts = value.upper().split('TS')
            pt = safe_eval(parts[0].replace('%', '')) if parts[0] else None
            ts = safe_eval(parts[1].replace('%', '')) if len(parts) > 1 else None
            return {'PT': pt, 'TS': ts}
        except SafeEvalError:
            return None


def parse_trailing_stop_string(value: str) -> Optional[Dict[str, Any]]:
    """Parse trailing stop string like '50%TS5%' or '30TS0'."""
    if not value or 'TS' not in str(value).upper():
        return None
    try:
        parts = str(value).upper().split('TS')
        pt = safe_eval(parts[0].replace('%', '')) if parts[0] else None
        ts = safe_eval(parts[1].replace('%', '')) if len(parts) > 1 else None
        return {'PT': pt, 'TS': ts}
    except SafeEvalError:
        return None


def safe_eval_or_none(value: str) -> Optional[Union[int, float]]:
    """Try to evaluate as math expression, return None on failure."""
    if not value:
        return None
    try:
        return safe_eval(value)
    except SafeEvalError:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
