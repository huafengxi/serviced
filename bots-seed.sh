#!/usr/bin/env bash
# bots-seed.sh — 守护型 bot 声明源 → 运行态 only-if-missing seed
#
# 声明源 = ~/m/bots/daemon/<name>/{spec.json,prompt.md}（被追踪，修改走 git）；运行态 =
# ~/m/agents/bot/<name>/…（agents-sync 管理，不入 git；agentd/agentctl 是运行态写者）。
# 本脚本只在运行态文件**缺失**时原子恢复（tmp+rename），存在则跳过——绝不覆盖运行态
# 文件，也不做声明源→运行态的周期性强同步（那会重现 agents-sync 与 git 双写者的竞态
#）。
#
# seed 面：
#   + spec.json  进程声明（必需；glob 锚点 = 有它才算一份声明）
#   + prompt.md  会话型常驻 bot 的初始引导（**可选**：缺失即保持裸启动 = 设计
#                决策 6 的「初始引导可选」路径；脚本守护型 bot 不给它，运行态无 session）。
#                消费方 = agentd/pi-rpc-wrap.py 的初始投递：blank 新代 spawn 时投递一次
#                会话已有 user 消息则幂等跳过。属 spawn 期读进会话的注入面 ⇒ 改动须递增
#                env/services.yml 里 agentd 的 version。
#
# 调用点：① make bots.seed（手动/验证）；② svc/agentd-loop.sh 拉起 runner 前
# （fresh clone 后自愈）。
#
# 不按 ~/m/env/host-id 过滤：声明的 bot 可带任意 host（声明源里的 host 就是它的目标机）
# 不过滤也无害——agentd 只监督 host=自己的 bot，其它机器多出几份运行态 spec 不会被拉起
# （他机 host 的 bot 靠 agents-sync 把它那台机需要的文件带过去）。
#
# bash 3.2 兼容（mac 自带）：不用关联数组 / mapfile / ${var,} 等新特性。
set -u

WS="$(cd "$(dirname "$0")/.." && pwd)"
# Declaration source: <ws>/bots/daemon/<name>/{spec.json,prompt.md} by default.
# Override for a different layout — this script ships no workspace taxonomy.
SRC_DIR="${BOTS_SEED_SRC:-$WS/bots/daemon}"
DST_BASE="$WS/agents/bot"

# 每份声明里可 seed 的文件名（顺序无意义；缺失的跳过）
SEED_FILES="spec.json prompt.md"

seed_file() {
  # $1 = 声明源文件，$2 = 运行态目标文件
  _src="$1"
  _dst="$2"
  if [ -e "$_dst" ]; then
    echo "exists-skip: $_dst"
    return 0
  fi
  mkdir -p "$(dirname "$_dst")"
  _tmp="$_dst.tmp.$$"
  cp "$_src" "$_tmp" && mv "$_tmp" "$_dst"
  echo "seeded: $_dst"
}

for src_spec in "$SRC_DIR"/*/spec.json; do
  [ -e "$src_spec" ] || continue
  dir="$(dirname "$src_spec")"
  name="$(basename "$dir")"
  for f in $SEED_FILES; do
    src="$dir/$f"
    [ -e "$src" ] || continue
    seed_file "$src" "$DST_BASE/$name/$f"
  done
done
