"""收件核销服务命令行。

用法示例::

    python3 -m recon.cli --base http://127.0.0.1:8040 --state ./recon_state run
    python3 -m recon.cli --state ./recon_state status
    python3 -m recon.cli --state ./recon_state adjust EAST 2026-11-20

``run`` 每轮次调用一次即可，跨日/跨进程重复调用安全：归档 token 幂等、会计
请求号幂等、游标与回条状态全部落盘。
"""
from __future__ import annotations

import argparse
import json

from .engine import ReconciliationEngine


def main() -> None:
    parser = argparse.ArgumentParser(description="换桩收件核销服务")
    parser.add_argument("--base", default="http://127.0.0.1:8040", help="联调来源服务地址")
    parser.add_argument("--state", default="recon_state", help="状态目录（可跨重启续做）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="执行一轮收件核销")
    sub.add_parser("status", help="输出站日/分表完整度、追单证据与会计交接状态")
    adjust = sub.add_parser("adjust", help="人工确认并发送一笔待处理调账")
    adjust.add_argument("meter")
    adjust.add_argument("window")
    args = parser.parse_args()

    engine = ReconciliationEngine(args.base, args.state)
    if args.command == "run":
        result = engine.run_once()
    elif args.command == "status":
        engine.sync_shift()
        engine.refresh_static()
        result = engine.status()
    else:
        result = engine.confirm_adjustment(args.meter, args.window)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
