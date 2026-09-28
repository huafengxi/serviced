#!/usr/bin/env bash
# scheduler-loop.sh — agentd 调度半边的常驻循环（由 make scheduler.start 管理）
#
# 行为：循环运行 agentd/scheduler.py（独立调度方，多机阶段 2 去合署后的形态）；
# scheduler 退出（崩溃/异常）则记日志、睡 2s 后自动拉起。
# SIGTERM/SIGINT 优雅退出（trap 带走子进程）。形态照抄 agentd-loop.sh。
#
# 部署语义（设计 DESIGN-multimachine §3.4/§6 阶段 2）：
#   - 调度方全局唯一实例（协议硬约束）：本机合署守护（旧形态）停止后才可启动本服务；
#     绝不允许与本服务同时运行任何内嵌调度的守护（双调度器 = 双放行风险）。
#   - --all-hosts 全局视图：为所有机器的任务放行（多机终态：调度器留本机、给他机派任务）；
#     占位/资源/能力表本就是全局语义。单机未切换形态去掉该参数即回到按本机过滤。
#   - 无状态每轮重扫（协议 §14.5）：崩溃/重启不丢调度状态，拉起即追平。
#
# 日志：scheduler 自身经 --log-file 写 run/logs/scheduler.log；loop 自身的启停记录也写同一文件。
set -u

WS="$(cd "$(dirname "$0")/.." && pwd)"
SCHEDULER="$WS/agentd/scheduler.py"
LOG="$WS/run/logs/scheduler.log"
mkdir -p "$WS/run/logs"

CHILD=0

on_term() {
  echo "$(date '+%F %T') [scheduler-loop] 收到 TERM/INT，优雅退出 (pid $$)" >> "$LOG"
  [ "$CHILD" -ne 0 ] && kill "$CHILD" 2>/dev/null
  exit 0
}
trap on_term TERM INT

echo "$(date '+%F %T') [scheduler-loop] 启动 (pid $$)，由 make scheduler.start 管理" >> "$LOG"

# 机器身份：放行门禁第四条件（目标主机判活）需要本机身份集合——
# spec.host 已在登记时物化落盘（登记机规范名；缺 host = 无人认领
# 永久排队，认领/调度两侧均无「缺省→本机」兜底）；判活候补锁名取自本机身份（规范名 +
# hostname 别名），口径照抄 agentd-loop.sh（env/host-id 映射：hostname → 规范名，未命中回退 hostname）。
# --all-hosts 下不影响候补过滤（全局视图），只供判活与 --all-hosts 关闭时的兑底形态。
HOST_ID="$(awk -v h="$(hostname)" '!/^[[:space:]]*#/ && $1==h {print $2; exit}' "$WS/env/host-id" 2>/dev/null || true)"
[ -n "$HOST_ID" ] || HOST_ID="$(hostname)"

# 占位上限 10（用户 08-28 指令）：参数化覆盖代码缺省 4
# 不改 agentd/scheduler.py 的 DEFAULT_MAX_CONCURRENT。
MAX_CONCURRENT=10

while :; do
  python3 "$SCHEDULER" --root "$WS" --all-hosts \
    --host "$HOST_ID" --aliases "$(hostname)" \
    --max-concurrent "$MAX_CONCURRENT" \
    --interval 0.5 --log-file "$LOG" --log-level INFO >> "$LOG" 2>&1 &
  CHILD=$!
  wait "$CHILD" 2>/dev/null
  rc=$?
  CHILD=0
  echo "$(date '+%F %T') [scheduler-loop] scheduler 退出 rc=${rc}，2s 后重启" >> "$LOG"
  # 分片 sleep，保证 TERM 能被及时处理（同 agentd-loop.sh）
  sleep 2 &
  CHILD=$!
  wait "$CHILD" 2>/dev/null
  CHILD=0
done
