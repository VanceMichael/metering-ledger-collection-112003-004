"""换桩收件核销服务。

跨轮次（shift）持续运行的收件核销端：从两家运营商的实时日账/增量流水/归档、
施工分表读数与会计交接台取证，把可核实的用量按分表窗口交给会计，并持续追踪
仍缺证据的电量。所有状态落盘，重启后从同一状态目录继续工作。
"""

from .engine import ReconciliationEngine

__all__ = ["ReconciliationEngine"]
