#!/usr/bin/env bash
# heartbeat-loop.sh — heartbeat 常驻循环（替代 cron；2026-09-03 起由 agentd 监督：
# 进程型 bot bot/heartbeat-loop/，spec.json + restartPolicy=auto 崩溃自愈
# 停止/恢复 = agentctl control bot/heartbeat-loop pause|restart，见 DISPATCH.md §4「守护型 bot」）
#
# 行为：启动时不立即执行，先等到下一个北京时间 11:00（本机时区即 Asia/Shanghai）
# 到点执行 assistant/heartbeat.sh，然后睡到次日 11:00，无限循环。
# 日志追加到 run/logs/heartbeat.log（每次启动/唤醒/执行/下次触发时间）。
# SIGTERM/SIGINT 优雅退出（trap）。
#
# 跨平台：日期计算同时兼容 GNU date（date -d）与 macOS BSD date（date -j/-r/-v）
# 启动时一次性检测方言（DATE_KIND）。任何日期异常都有日志 + 60s 兜底，绝不空转。
set -u

WS="$(cd "$(dirname "$0")/.." && pwd)"
LOG=$WS/run/logs/heartbeat.log
mkdir -p "$WS/run/logs"

# --- date 方言检测：GNU date 支持 -d，BSD date 不支持 ---
if date -d @0 +%s >/dev/null 2>&1; then
  DATE_KIND=gnu
else
  DATE_KIND=bsd
fi

# epoch -> "YYYY-MM-DD HH:MM:SS"
epoch_to_human() {
  if [ "$DATE_KIND" = gnu ]; then
    date -d "@$1" '+%F %T'
  else
    date -r "$1" '+%F %T'
  fi
}

# 明天的日期（YYYY-MM-DD）
tomorrow_fmt() {
  if [ "$DATE_KIND" = gnu ]; then
    date -d tomorrow +%F
  else
    date -v+1d +%F
  fi
}

# "<DATE> 11:00:00" -> epoch
date9_epoch() {
  if [ "$DATE_KIND" = gnu ]; then
    date -d "$1 11:00:00" +%s
  else
    date -j -f '%F %T' "$1 11:00:00" +%s
  fi
}

# 校验参数是合法整数
is_int() {
  case "$1" in
    ''|*[!0-9-]*) return 1 ;;
    *) return 0 ;;
  esac
}

CHILD=0

on_term() {
  echo "$(date '+%F %T') [heartbeat-loop] 收到 TERM/INT，优雅退出 (pid $$)" >> "$LOG"
  # 若正在 sleep 或执行 heartbeat.sh，终止子进程
  [ "$CHILD" -ne 0 ] && kill "$CHILD" 2>/dev/null
  exit 0
}
trap on_term TERM INT

# 异常兜底：写日志 + 睡 60s（后台 sleep + wait，保证 TERM 可打断），绝不空转
fallback_sleep() {
  echo "$(date '+%F %T') [heartbeat-loop] 日期计算异常($1)，60s 后重试" >> "$LOG"
  sleep 60 &
  CHILD=$!
  wait "$CHILD" 2>/dev/null
  CHILD=0
}

# 下一个 11:00 的 epoch（今日未到取今日，已过取明日）；失败返回空串
next_fire() {
  local now today9 tmr
  now=$(date +%s)
  today9=$(date9_epoch "$(date +%F)" 2>/dev/null)
  if ! is_int "$today9"; then
    echo ""
    return
  fi
  if [ "$now" -lt "$today9" ]; then
    echo "$today9"
  else
    tmr=$(tomorrow_fmt 2>/dev/null) || { echo ""; return; }
    date9_epoch "$tmr" 2>/dev/null || echo ""
  fi
}

echo "$(date '+%F %T') [heartbeat-loop] 启动 (pid $$)，由 agentd 监督（bot/heartbeat-loop/）(date=$DATE_KIND)" >> "$LOG"

while :; do
  target=$(next_fire)
  if ! is_int "$target"; then
    fallback_sleep "next_fire=$target"
    continue
  fi
  echo "$(date '+%F %T') [heartbeat-loop] 下次触发: $(epoch_to_human "$target")" >> "$LOG"

  # 分片 sleep（每段最多 60s），保证 TERM 能被及时处理
  while :; do
    now=$(date +%s)
    if ! is_int "$now"; then
      fallback_sleep "now=$now"
      break
    fi
    [ "$now" -ge "$target" ] && break
    rem=$((target - now))
    [ "$rem" -gt 60 ] && rem=60
    sleep "$rem" &
    CHILD=$!
    wait "$CHILD" 2>/dev/null
    CHILD=0
  done

  echo "$(date '+%F %T') [heartbeat-loop] 到点，执行 assistant/heartbeat.sh" >> "$LOG"
  "$WS/assistant/heartbeat.sh" >> "$LOG" 2>&1 &
  CHILD=$!
  wait "$CHILD" 2>/dev/null
  rc=$?
  CHILD=0
  echo "$(date '+%F %T') [heartbeat-loop] heartbeat.sh 执行完毕 exit=$rc" >> "$LOG"
done
