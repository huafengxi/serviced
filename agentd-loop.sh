#!/usr/bin/env bash
# agentd-loop.sh — agentd 新调度系统守护的常驻循环（由 make agentd.start 管理）
#
# 行为：循环运行 agentd/runner.py（新协议中央守护，§7.2/§15.5；enable.json 门禁内置
# 无开关（--scheduled 已移除）：
# 只消费调度方（独立服务，见 svc/scheduler-loop.sh）写的 enable.json，§14.7 一期 FIFO 串行）；runner 退出（崩溃/异常）则记日志、睡 2s 后自动拉起——进程级崩溃自愈。
# SIGTERM/SIGINT 优雅退出（trap 带走子进程）。形态沿用旧调度守护的常驻循环：
# 只管 <root>/agents/ 一棵树，无共享状态。
#
# --root 语义（已核实 T1-3）：树布局 = <ROOT>/agents/<id>
# （proto.agent_dir = root/agents/<id>），工具层 core.ts 写 ~/m/agents/<taskId>，
# 故 root = ~/m（$WS）——不是 ~/m/agents（那是计划 §2.2/README 的笔误，已随本任务更正）。
#
# 日志：runner 自身经 --log-file 写 run/logs/agentd.log；loop 自身的启停记录也写同一文件。
set -u

WS="$(cd "$(dirname "$0")/.." && pwd)"
RUNNER="$WS/agentd/runner.py"
LOG="$WS/run/logs/agentd.log"
mkdir -p "$WS/run/logs"

CHILD=0

on_term() {
  echo "$(date '+%F %T') [agentd-loop] 收到 TERM/INT，优雅退出 (pid $$)" >> "$LOG"
  [ "$CHILD" -ne 0 ] && kill "$CHILD" 2>/dev/null
  exit 0
}
trap on_term TERM INT

echo "$(date '+%F %T') [agentd-loop] 启动 (pid $$)，由 make agentd.start 管理" >> "$LOG"

# 守护型 bot 声明源 seed：agents/ 全量去追踪后，声明源 =
# bots/daemon/<name>/spec.json；运行态缺失时恢复（only-if-missing，绝不覆盖）
# 保证 fresh clone 后自愈。seed 失败不阻塞守护启动（仅记日志）。
bash "$WS/svc/bots-seed.sh" >> "$LOG" 2>&1 || echo "$(date '+%F %T') [agentd-loop] bots-seed 失败（不阻塞守护启动）" >> "$LOG"

# 机器身份（多机阶段 0；设计 DESIGN-multimachine §3.3；
# host-id 改映射文件）：spec.host 一律用规范名（env/ssh-hosts
# 的 Host 别名）；登记侧写规范名，故认领侧身份 = 规范名 + 本机 hostname 别名。
# 规范名来源 = env/host-id 映射文件（hostname → 规范名，入库一份全机器共用）按 $(hostname)
# 查表；未命中/文件缺失回退 hostname（不阻塞守护启动）。
HOST_ID="$(awk -v h="$(hostname)" '!/^[[:space:]]*#/ && $1==h {print $2; exit}' "$WS/env/host-id" 2>/dev/null || true)"
[ -n "$HOST_ID" ] || HOST_ID="$(hostname)"

while :; do
  python3 "$RUNNER" --root "$WS" --host "$HOST_ID" --aliases "$(hostname)" \
    --log-file "$LOG" --log-level INFO >> "$LOG" 2>&1 &
  CHILD=$!
  wait "$CHILD" 2>/dev/null
  rc=$?
  CHILD=0
  echo "$(date '+%F %T') [agentd-loop] runner 退出 rc=${rc}，2s 后重启" >> "$LOG"
  # 分片 sleep，保证 TERM 能被及时处理
  sleep 2 &
  CHILD=$!
  wait "$CHILD" 2>/dev/null
  CHILD=0
done
