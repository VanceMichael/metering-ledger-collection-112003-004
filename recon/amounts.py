"""定点电量与时分区间的基础工具。

电量为十进制定点千瓦时（字符串），统一转 Decimal 处理，输出时量化回两位小数；
营业日内时分按左闭右开比较。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

TWO_PLACES = Decimal("0.01")


def D(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def q2(value) -> str:
    """量化为两位小数字符串，供会计能量字段使用。"""
    return str(D(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def tmin(a: str, b: str) -> str:
    return a if a <= b else b


def tmax(a: str, b: str) -> str:
    return a if a >= b else b


def overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> int:
    """两个左闭右开时分区间是否相交。"""
    return start_a < end_b and start_b < end_a


def sum_decimal(values: Iterable) -> Decimal:
    total = Decimal("0")
    for value in values:
        total += D(value)
    return total
