#!/usr/bin/env python3
"""clean-make.py — 从干净环境执行 make（根 AGENTS.md「重启纪律」②）。

服务/任务进程内直接 `make <name>.stop/start` 会把调度身份变量
（`AGENTD_TASK`/`AGENT_SELF`/`AGENT_HOME`/`PI_*`/`DISPATCH_*` …）带进被启动的服务
进程。本薄壳先按 `agentd/envscrub.py`（名单单一事实源）洗刷环境、再执行 make，
并在执行前自检：仍有身份变量残留 → 拒绝执行。

用法: python3 svc/clean-make.py <target> [<target> ...] [--timeout N] [--dry-run]
  例: python3 svc/clean-make.py rshd.stop rshd.start
      python3 svc/clean-make.py svc.status
"""
import argparse
import os
import subprocess
import sys

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WS, "agentd"))
import envscrub  # noqa: E402  spawn 环境洗刷口径单点

# 画像/agent 标记（DISPATCH_PROFILE / AI_AGENT）已收进 envscrub.ENV_SCRUB_EXACT
# 单一事实源（攒批 4：三个调用方同享，不再只在本薄壳里额外 pop）
# 下面的前缀自检仍是兜底（DISPATCH/AI_ 前缀命中即拒绝执行 make）。
LEAK_PREFIXES = ("AGENT", "DISPATCH", "PI_", "SESSIOND", "AI_")


def clean_env():
    env = envscrub.scrub_env(keep=frozenset())
    leaked = sorted(k for k in env if k.startswith(LEAK_PREFIXES))
    print("scrubbed keys present? %s" % leaked, flush=True)
    if leaked:
        sys.exit("refusing to run make with a polluted environment")
    return env


def main():
    ap = argparse.ArgumentParser(
        description="run make targets from a scrubbed (clean) environment")
    ap.add_argument("targets", nargs="+", help="make target(s), e.g. rshd.stop")
    ap.add_argument("--timeout", type=int, default=180,
                    help="seconds before make is killed (default 180)")
    ap.add_argument("--dry-run", action="store_true",
                    help="only print the scrub self-check and the command")
    args = ap.parse_args()

    env = clean_env()
    cmd = ["make"] + args.targets
    print("$ (clean env, cwd=%s) %s" % (WS, " ".join(cmd)), flush=True)
    if args.dry_run:
        return 0
    try:
        return subprocess.run(cmd, cwd=WS, env=env,
                              timeout=args.timeout).returncode
    except subprocess.TimeoutExpired:
        print("make timed out after %ds" % args.timeout, file=sys.stderr)
        return 124


if __name__ == "__main__":
    sys.exit(main())
