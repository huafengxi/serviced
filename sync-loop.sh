#!/usr/bin/env bash
# serviced/sync-loop.sh — 常驻纠偏循环：每 SERVICED_SYNC_INTERVAL 秒（缺省 60）跑一次同仓的
# `serviced.py sync`，把本机现网纠偏到注册表声明（两类 drift：期望态启停 + 在线服务版本 stale
# 的 stop+start）。以哪个服务名跑、跑在哪台机，一律由调用方注册表声明（本仓不含部署面值）。
#
# **为什么是「壳 + 子进程」而不是给 sync 加一个 --loop**：sync 会判本服务自己 stale 并 stop 它，
# 而 stop 对进程组组长走 killpg（serviced.py Invariant 2）⇒ 与本 loop **同组**的 sync 会被连带
# 打死在「已停、未起」的半程，本服务就此长眠（没人再拉起它）。故 sync 一律在**独立进程组**里跑：
# `set -m`（job control）让后台子进程自成一组 ⇒ 它活得过本 loop 的死亡，把新版本拉起来。
# 不用 `setsid` 命令（macOS 无）；不追杀在飞的 sync（追杀 = 上面那个自杀形态）。
# 代价（记在案，不加机制）：① 手工 stop 本服务时，在飞的那一轮 sync 会跑完；② 若那一轮正把本
# 服务按 stale 重启，它会照起回来 ⇒ **要它长期不跑 = 注册表里把本机声明改 `desired: offline`**
# （offline 的语义是「stop，绝不重启」）。
#
# 日志：stdout/stderr 由执行层接到 run/logs/<服务名>.log。无 drift 的轮次不记（每轮都记 = 一天
# 上千行同一句话），静默判据 = sync 的单行输出 `no drift, nothing to do`；该措辞若改 ⇒ 退化成
# 每轮都记（多写日志，不漏事）。每 SERVICED_SYNC_BEAT 秒（缺省 3600）记一行 alive 证明在跑。
# 一轮 sync 失败（注册表 config error、探针异常等）⇒ 每轮照记，响亮失败不退避。
#
# ⚠ 本文件的字串插值一律写 `${name}` 形态（即使后面接的是 ASCII）：**变量名紧邻多字节字符时，
# bash 3.2（macOS 自带）会把后继字节并进变量名** ⇒ `set -u` 下整个 loop 死在「有东西要报」的
# 那条分支上（实测报文 = `rc<乱码字节>: unbound variable`；bash 4.4/5.1 不受影响 ⇒ 只在最老
# 的那台机上发作，且发作时机正好是唯一需要它说话的时候）。
# 每轮的 sync 输出先落一个**逐轮唯一**的临时文件（run/sync-loop.out.XXXXXX）再由 loop 转写日志、
# 随即删除。唯一性是正确要求，不是为了防泄文件：自指重启时旧 loop 的 sync 子进程与新 loop 会
# 同时在写（实测：共用固定路径时两边各持自己的 offset ⇒ 新 loop 的 truncate 与旧 sync 的续写
# 互盖，文件里只剩 NUL 填隙的残渣）。代价 = **被打死那一轮的输出文件不会被删**，而这正是想要的：
# **自指重启那一轮 loop 自己打不出日志**（它已被 stop）⇒ 事后读 run/sync-loop.out.* 的残留文件
# 才知道上一轮干了什么（每次自指重启残留一枚，数量有界；run/ 不入 git）。
#
# 节奏：串行（sync 阻塞跑完才睡下一轮）⇒ 重型服务的一轮纠偏不会与下一轮重叠。
# 覆盖不到的面：整机重启后的零服务态（本 loop 自己也没跑）——恢复靠调用方的引导配方。
#
# **本文件的 while 循环必须是最后一个构造**：bash 边读边执行，运行中改本文件会让续读位置错位；
# 在 `done` 之后追加内容 ∨ 给循环加 `break`，都必须伴随重启。

set -u
set -m  # job control：后台子进程自成进程组（正确性要求，缘由见头注）

WS="$(cd "$(dirname "$0")/.." && pwd)"
SYNC="$WS/serviced/serviced.py"
QUIET_LINE="no drift, nothing to do"

_posint() {  # 非法值回退缺省：一个坏值不能让 sleep 立刻返回 ⇒ 变成 sync 热循环
  local v="$1" d="$2"
  case "$v" in ''|*[!0-9]*) printf '%s' "$d"; return;; esac
  [ "$v" -gt 0 ] && printf '%s' "$v" || printf '%s' "$d"
}
INTERVAL="$(_posint "${SERVICED_SYNC_INTERVAL:-}" 60)"
BEAT="$(_posint "${SERVICED_SYNC_BEAT:-}" 3600)"
cd "$WS" || exit 1
mkdir -p "$WS/run" || exit 1

SLEEP_PID=0
CYCLES=0
ACTED=0
LAST_ACTED="-"
LAST_BEAT="$(date +%s)"

on_term() {
  echo "$(date '+%F %T') [sync-loop] 收到 TERM/INT，退出 (pid $$)；在飞的一轮 sync 不追杀（头注：追杀 = 半程自杀）"
  [ "$SLEEP_PID" -ne 0 ] && kill "$SLEEP_PID" 2>/dev/null
  exit 0
}
trap on_term TERM INT

echo "$(date '+%F %T') [sync-loop] 启动 (pid $$) interval=${INTERVAL}s beat=${BEAT}s sync=${SYNC}"

while :; do
  out_file="$(mktemp "$WS/run/sync-loop.out.XXXXXX" 2>/dev/null)" || out_file="$WS/run/sync-loop.out.$$"
  python3 "$SYNC" sync > "$out_file" 2>&1 &
  pid=$!
  wait "$pid" 2>/dev/null
  rc=$?
  out="$(cat "$out_file" 2>/dev/null)"
  rm -f "$out_file"

  CYCLES=$((CYCLES + 1))
  if [ "$rc" -eq 0 ] && [ "$out" = "$QUIET_LINE" ]; then
    :  # 无 drift：静默（判据见头注）
  else
    ACTED=$((ACTED + 1))
    LAST_ACTED="$(date '+%F %T')"
    echo "$(date '+%F %T') [sync-loop] cycle #${CYCLES} rc=${rc}（有动作 ∨ 非静默输出）"
    printf '%s\n' "$out" | sed 's/^/  | /'
  fi

  now="$(date +%s)"
  if [ $((now - LAST_BEAT)) -ge "$BEAT" ]; then
    LAST_BEAT="$now"
    echo "$(date '+%F %T') [sync-loop] alive: cycles=${CYCLES} acted=${ACTED} last=${LAST_ACTED} interval=${INTERVAL}s
  fi

  # 后台 sleep + wait：TERM 能立刻打断（同 agentd/loop.sh 的形态）
  sleep "$INTERVAL" &
  SLEEP_PID=$!
  wait "$SLEEP_PID" 2>/dev/null
  SLEEP_PID=0
done
