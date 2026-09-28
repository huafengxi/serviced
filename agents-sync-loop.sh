#!/usr/bin/env bash
# agents-sync-loop.sh — agents/ 跨机同步链路的常驻监督循环。跑在**各节点机本机**，
# 由该机自己的 `make agents-sync.start` 拉起（每台只跑本机链路，无跨机拉起）。
#
# 拓扑：星型，hub = **中立目录 dev:/data/shared/agents**（零原件：hub 侧一切落盘带
# replica 组，无任何进程在 hub 写原件），与任何写者面物理分离。dev/nv1/nv2/mac 四机
# 对等（含 dev，dev 上 ssh 自连），每机一条链路：
# `ssh-sync.py watch <本机 ~/m/agents> dev:/data/shared/agents` 双向。
#
# 传输契约（权威 = dsync/ssh-sync.py docstring + `@dsync#watch-cost`）：
# + 不开 --delete（删除不跨机传播；树内清理走 dsync/gc.py delete-list）；
# + 覆盖安全由**文件系统属组闸门**结构性保证，全程无时间戳裁决：replica 组 = 同步
#   落盘的副本，未标记 = 本机原件（每个文件只有一个可能来源，单写者纪律）；
# + **每轮都协商整棵树**（push 走白名单 = 本地未标记文件，pull 走黑名单 = 本地未标记
#   文件受保护），跳过判据 = rsync 自己的 `-c` 内容校验和（字节有差异即传输，时间戳
#   不参与任何裁决；不用 -I：它会在接收端逐候选整文件重写）；
# + 检测器只回答「有没有变更」，不记路径、不分类事件、不做快照 ⇒ 一轮的成本与变更量
#   无关，轮次节奏由 `--min-cycle`（速率下限）与 `--interval`（强制周期 = 任何变更的
#   重新锚定上界，deadline 本身即触发，树完全静默也照跑）共同界定；
# + 同步面排除 = `*.pending`（receiver 两阶段送达的在飞记账件）**+ gc delete-list 路径**；
#   排除面无运行时开关（安全语义，非性能旋钮）；
# + 落盘一律 --chown=:replica 打标（组名在接收端本地解析，各机 gid 无需一致）；
#   rsync 标志 -rlptDvc（-a 去掉 -o -g：属组只由 --chown 一处决定）；
# + 探活锁文件（agents/run/agentd.<host>.lock）由闸门天然双向正确（各机写自己的锁：
#   未标记→push 包含、pull 排除），不需要排除项。
# 存量树首跑前须先打一次标：dsync/replica-tag.py（干跑留痕后 --apply）。
#
# 监督：watch 进程退出（崩溃/断链）则记日志、有界退避后自动拉起——连续「短命退出」
# （存活 <10s）睡眠翻倍至上限 60s，某次存活 ≥60s 即复位回 2s（避免闸门类必然失败变成
# 2s 无限热循环刷日志）；SIGTERM/SIGINT 优雅退出（trap 带走子进程）。形态照抄
# agentd-loop.sh。
#
# **replica 补充组要求 + 自愈 + 响亮失败**：ssh-sync.py 的属组闸门要求**进程凭据**里有
# replica 组（落盘一律 --chown=:replica，本地 chgrp 自检 EPERM 即 die）。/etc/group 有该组、
# `id $USER` 也含它，仍可能缺：服务树若从**早于建组的会话血统**（旧 tmux server 等）重启，
# 进程的 /proc/<pid>/status Groups 里没有那个 gid ⇒ watch 起来就 die（曾实测 2s 崩溃循环
# 18h+、日志 18.8MB、dev↔hub 双向断）。故本脚本在进循环前做一次性判定：缺组且有 `sg` ⇒
# 记一行日志后经 `sg replica` **原地 exec 重新拉起自身**（任何血统的启动方都能得到带组的
# 进程）；缺组且无 `sg`（macOS 等）∨ 已自愈过一次仍缺组 ⇒ 打**一行** ERROR（含可直接
# 复制的修法命令）后 `exit 1`，**绝不进循环**。判定一律用组名（`id -Gn`）——gid 形态要先
# `getent group` 而 macOS 无 getent，会让整块判定在 mac 上跳过（既不自愈也不响亮失败 =
# `svc.status` 假绿、链路实死）。
# exec 形态不破 match 语义（实测）：`sg` 与其 `$SHELL -c` 都是单条简单命令 ⇒ 整链原地 exec
# **塌缩成一个进程**，pid/pgid/sid/cwd 全不变、cmdline 仍逐字 `bash svc/agents-sync-loop.sh`
# ⇒ services.yml 的 `match`/`stop_match`、svc.py 的 pidfile（pid+lstart）身份与 killpg 停服
# 语义都不破。
# **已知影响**：经 sg（setgid）起的进程及其全部子孙是 non-dumpable ⇒ 同用户也读不到
# /proc/<pid>/fd，`lsof <日志路径>` 对它们返回空（/proc/<pid>/status、cmdline 仍可读 ⇒
# 判活、核 Groups、匹配 cmdline 不受影响）。任何「谁持有这个文件」的判据对这类进程一律
# 不可信，故 `make logs.trim` 对 run/logs/*.log 一律原地 truncate（svc.py:cmd_trim）。
#
# ssh 配置：节点机通常没有 env/.live/ssh-hosts（gitignored），故用 SSH_SYNC_CONFIG 指向
# ~/.ssh/config（其中须有 Host dev 别名指向 hub）。
# python3 解析：watchdog 只装在 ~/miniconda3 —— 若 python3 解析到 macOS 的
# /opt/homebrew/bin/python3，本地检测会从 watchdog/FSEvents 掉到「无后端」（只剩
# --interval 驱动）。该解析由 svc.py 给服务子进程的规范 PATH 保证（口径 =
# svc/README.md 的 svc.py 条目）：conda 的 bin 在其前缀里优先于 homebrew。rsync 的解析
# 固化在 ssh-sync.py 内部（优先 /usr/local/bin/rsync + --rsync-path），不依赖此处 PATH。
#
# 日志：$WS/run/logs/agents-sync.log（watch 自身输出也写同一文件）。
# **本文件的 while 循环必须是最后一个构造**：bash 边读边执行，运行中改本文件会让
# 续读位置错位；将来在 `done` 之后追加内容 ∨ 给循环加 `break`，都必须伴随重启。

WS="$(cd "$(dirname "$0")/.." && pwd)"
SYNC="$WS/dsync/ssh-sync.py"
LOG="$WS/run/logs/agents-sync.log"
mkdir -p "$WS/run/logs"

# --- replica 补充组：自愈（一次）∨ 响亮失败（头注有完整缘由与实测） -------------------
_gid_die() {  # 一行 ERROR（根因 + 可直接复制的修法）后退出，绝不进重拉循环
  local msg
  msg="$(date '+%F %T') [agents-sync-loop] ERROR: 启动会话凭据缺 ${REPLICA_GROUP} 补充组，ssh-sync.py 属组闸门会拒绝运行（$1）。修法（复制执行，stop/start 分两次）: sg ${REPLICA_GROUP} -c \"python3 svc/clean-make.py agents-sync.stop\" 然后 sg ${REPLICA_GROUP} -c \"python3 svc/clean-make.py agents-sync.start\"；或 sudo usermod -aG ${REPLICA_GROUP} \$USER 后从**新登录会话**重启。不进入重拉循环。"
  # 服务态下 svc.py 已把本进程 stderr 接到同一个日志文件（inode 相同）⇒ 只写 stderr
  # 一次，避免同一行在日志里重复；前台运行时两处都写。（该 test 上不能挂
  # 2>/dev/null：重定向会把 /proc/self/fd/2 自己改掉，实测则判假、两处都写。）
  if [ /proc/self/fd/2 -ef "$LOG" ]; then
    echo "$msg" >&2
  else
    echo "$msg" >> "$LOG" 2>/dev/null
    echo "$msg" >&2
  fi
  exit 1
}

# 成员判定用**组名**（`id -Gn`）而非 gid：gid 形态要先 `getent group` 取到 gid，而 macOS
# 既无 getent 也无 sg ⇒ 整块判定在 mac 上跳过，既不自愈也不响亮失败（status 假绿、链路实死）。
# 名字形态四机一致（各机 gid 本就不同，跨机共识只有组名这一个字符串）。
REPLICA_GROUP=replica
if ! id -Gn 2>/dev/null | tr ' ' '\n' | grep -qx "$REPLICA_GROUP"; then
  # 自愈只允许一次（sg 原地 exec 后不留常驻父进程 ⇒ 判父进程无效，实测）：env 标记能穿过 sg。
  # 自愈成功后组名就在 `id -Gn` 里 ⇒ 上面的判定直接通过，不会二次进入本分支。
  if [ -n "${AGENTS_SYNC_SG_REEXEC:-}" ]; then
    _gid_die "已经 sg 自愈过一次仍缺组"
  elif command -v sg >/dev/null 2>&1; then
    echo "$(date '+%F %T') [agents-sync-loop] 进程凭据缺 $REPLICA_GROUP 补充组，经 sg $REPLICA_GROUP 重新拉起自身（自愈，仅一次）" >> "$LOG"
    cd "$WS" || exit 1  # match 要求相对路径 + cwd=$WS
    exec sg "$REPLICA_GROUP" -c "env AGENTS_SYNC_SG_REEXEC=1 bash svc/agents-sync-loop.sh"
    _gid_die "exec sg 失败"
  else
    _gid_die "本机无 sg 可自愈（macOS 等）"
  fi
fi

# REMOTE 四机统一：dev 上 ssh 别名 dev = 本机（自连），其余机器 = 正常跨机；
# 节点机用户与 hub 相同。
REMOTE="dev:/data/shared/agents"

export SSH_SYNC_CONFIG="${SSH_SYNC_CONFIG:-$HOME/.ssh/config}"

PYTHON=python3

CHILD=0
SLEEP=2    # 快失败有界退避：存活 <10s 翻倍（上限 60s），某次存活 ≥60s 复位
CAPPED=0   # 上限提示只打一行

on_term() {
  echo "$(date '+%F %T') [agents-sync-loop] 收到 TERM/INT，优雅退出 (pid $$)" >> "$LOG"
  [ "$CHILD" -ne 0 ] && kill "$CHILD" 2>/dev/null
  exit 0
}
trap on_term TERM INT

echo "$(date '+%F %T') [agents-sync-loop] 启动 (pid $$)，由本机 make agents-sync.start 管理，python=$PYTHON" >> "$LOG"

while :; do
  # --debounce 0.5：首个未消费事件到达后 ≥0.5s 即触发一轮（静默窗语义在持续 churn 下
  #   会饿死；写方约定原子写，0.5s 聚合足够）。
  # --min-cycle 3：两轮之间的速率下限（一轮 = 整树 push + 整树 pull，实测约 1s）。
  # --interval 30：强制周期，即「任何变更的重新锚定上界」——检测器全失效也只有这个
  #   上界，树完全静默也照跑一轮。
  t0=$(date +%s)
  "$PYTHON" "$SYNC" watch "$WS/agents" "$REMOTE" \
    --debounce 0.5 --min-cycle 3 --interval 30 \
    >> "$LOG" 2>&1 &
  CHILD=$!
  wait "$CHILD" 2>/dev/null
  rc=$?
  CHILD=0
  alive=$(( $(date +%s) - t0 ))
  if [ "$alive" -lt 10 ]; then
    SLEEP=$(( SLEEP * 2 )); [ "$SLEEP" -gt 60 ] && SLEEP=60
    if [ "$SLEEP" -eq 60 ] && [ "$CAPPED" -eq 0 ]; then CAPPED=1; echo "$(date '+%F %T') [agents-sync-loop] watch 连续快失败（本次存活 ${alive}s <10s），退避至 60s，请查上面的错误" >> "$LOG"; fi
  elif [ "$alive" -ge 60 ]; then SLEEP=2; CAPPED=0; fi
  echo "$(date '+%F %T') [agents-sync-loop] watch 退出 rc=${rc}（存活 ${alive}s），${SLEEP}s 后重启" >> "$LOG"
  # 分片 sleep，保证 TERM 能被及时处理（同 agentd-loop.sh）
  sleep "$SLEEP" &
  CHILD=$!
  wait "$CHILD" 2>/dev/null
  CHILD=0
done
