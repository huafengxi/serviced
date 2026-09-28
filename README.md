# svc/ — 声明式服务生命周期层（declarative service lifecycle）

> **In English**: `svc.py` is the single execution layer for a declarative service
> registry (`services.yml`: `name` + `desired` + `version` + start definition). It
> implements start / stop / status / sync (reconcile) / log-trim, two independent
> drift judgements (desired-vs-actual and declared-version-vs-running-version), a
> channel-independent canonical `PATH` for service children, bounded liveness
> probes (process / HTTP / command-expect), and per-service pid+meta bookkeeping.
> Alongside it: `git-sync.py` (periodic ff-only pull of a repo set with dirty-tree
> and unpushed-commit guards, plus automatic sub-repo discovery), `clean-make.py`
> (run a lifecycle command from a scrubbed environment), `svc4web.py` (aggregate
> several machines' status into markdown for a dashboard), four small supervision
> loops, and a seed script for process-style daemon declarations. Everything is
> Python 3 stdlib + PyYAML, or POSIX shell. The rest of this file is the
> author's workspace manual (Chinese) and documents the exact semantics.

## 是什么

- `svc.py`：服务生命周期唯一执行层。读 `env/services.yml`（单一事实源：name + desired + version + 启动定义），执行 start/stop/status/sync/trim。
  版本 stale = 运行 meta（`run/pids/<name>.meta`）**首行**≠ 声明 version（meta 缺失不算 stale），一律计 drift 并由 sync 重启。Makefile 的 `<name>.start/.stop/.status`、`svc.status`、`svc.sync`、`logs.trim` 都是它的薄壳。
  meta 形态：**首行 = 裸 version 数字**（stale 判定与一切「读 version 数字」的消费者——心跳、调度员、`cat`/`head -1`/`awk '{print $1}'`——只读这一行，形态不变），其后每行一个 `key=value` 的**运行态读数**（start 确认成功后写入）：`exe` = 该进程实际解析到的可执行文件（realpath）、`interp` = 它的 `--version` 首行。取不到一律写字面量 `unknown`（不留空、不伪造），三种成因：无 pid（`wrapper: none` 且 `match` 无命中）∨ 进程 non-dumpable（`sg`/`newgrp` 起的 ⇒ `/proc/<pid>/exe` 不可读，现例 = `agents-sync`）∨ exe 不在「可执行 `--version`」的解释器/外壳名单内（名单外的二进制一律不执行——观测不得启动语义未知的程序，现例 = `clash` 的 `mihomo`）。
  读数**只作观测**：`svc.status` 在摘要行后给 `--- runtime ---` 块（每个在线服务一行 exe + interp；旧格式 meta 只给一行计数）、`<name>.status` 给一行读数、`status --machine` 只加键（`exe`/`interp`/`exe_expected`/`exe_warn`）。与声明面不符时在 STATE 追加 `⚠ exe≠声明解析`，**但不计 drift、不进 stale、`svc.sync` 不因它重启任何服务**（drift 判据仍只有 desired/actual 与 version 两类）。声明面 = 按 `cmd` + 规范 PATH 解析内核本该 exec 到的文件（shebang 脚本再解一层解释器）；解析不出 ∨ 读数为 `unknown` ∨ meta 是旧格式 ⇒ 只显示、不判定。
  服务子进程的 `PATH` 与调用通道无关：内置前缀（只收有点名现网消费者的目录，存在的才进、去重，nvm 的 glob 命中按自然版本序降序全进 ⇒ 首个命中胜出）接继承的 PATH 尾 ⇒ **只加不减**，一份 `services.yml` 在各机解析到同一解释器（工作区约定 = `~/miniconda3`）；继承 PATH 为空/缺失时补平台缺省地板（`os.defpath`），否则子进程连 shell 都解析不到。覆写口 `SVC_PATH_PREFIX`（冒号分隔，`~` 与 glob 均可）：非空 = **整体替换**内置前缀（不合并），空串 = 不加前缀。**整体替换 ⇒ 覆写值须自带服务孙进程所需的目录**（conda 的 `bin`、node 的 `~/.local/node/bin` ∨ nvm 的 `versions/node/*/bin`、`/opt/homebrew/bin`）：三个 loop 脚本已不再自补 PATH（`svc/{agentd,scheduler}-loop.sh` 的 `export` 与 `svc/agents-sync-loop.sh` 的 Darwin pin 均已删），裸名 `pi` 的解析只由这份规范 PATH 保证（实体 = `agentd/pi-rpc-wrap.py` 的 `pi_bin` 缺省 `"pi"`）。start 的 spawn、`status.cmd` 探针与 `stop_cmd` 三处共用这一份构造。
- services.yml 中服务的常驻循环脚本（由对应 `make <name>.start` 管理）：
  - `agentd-loop.sh` → agentd 守护循环（`agentd/runner.py`）
  - `scheduler-loop.sh` → agentd 调度半边（`agentd/scheduler.py --all-hosts`）
  - `agents-sync-loop.sh` → agents/ 跨机同步链路（各节点机本机拉起）
    - 启动前核进程凭据的 `replica` 补充组（ssh-sync.py 属组闸门要求）：缺组且有 `sg` ⇒ 经 `sg replica` 原地 exec 重新拉起自身（仅一次；pid/pgid/cmdline 不变，`match` 语义不破）；缺组且无 `sg` ∨ 自愈后仍缺组 ⇒ 一行 ERROR（含可复制修法）后 `exit 1`，不进重拉循环
    - watch 快失败有界退避：存活 <10s 睡眠翻倍至上限 60s（上限提示只打一行），存活 ≥60s 复位回 2s
    - 经 `sg` 起的进程 non-dumpable ⇒ `lsof`/`/proc/<pid>/fd` 对它全盲（status/cmdline 仍可读）；完整缘由 = 脚本头注
  - `git-sync.py` → 主职：主仓与全部子仓的周期性代码拉取（`git pull --ff-only`；脏树/本地领先则 SKIP，SKIP 行带 `behind=N` = 本轮没拉到的 origin 提交数，0 即零损失）；dev 上另承担镜像区周期性 fetch（GitHub → dev:~/git，后台线程 + 每镜像超时 ⇒ 挂起不拖延拉取）
    - 同步面自动发现：顶层含 `.git` 且被主仓 `.gitignore` 排除的目录即入面（登记的子仓必然要加 ignore 条目，否则主仓会把它当 gitlink ⇒ 名单自维护，与 Makefile `repo-list` 的 clone 名单分权、无需手工对齐）；`--repos` 覆盖
    - 输出面只有日志（`run/logs/git-sync.log`）：判定序与日志形态权威 = 脚本 docstring
- 进程型守护的监督循环（**不在 services.yml**，由工作区的 agent 守护监督）：
  - `heartbeat-loop.sh` → 每日心跳驱动器（到点执行工作区的 `assistant/heartbeat.sh`，然后睡到次日同一时刻）
  - （同族的信箱转发守护 `notify-user` 的实现体在主仓 `bots/notify-user/`：它含 IM 通道与收件人信息，不随本仓公开）
- `bots-seed.sh`：把**被追踪的进程声明源**（`spec.json` 必需 + `prompt.md` 可选）only-if-missing 原子 seed 到运行态目录（`make bots.seed`），**绝不覆盖运行态既有文件**。声明源目录 = `${BOTS_SEED_SRC:-<ws>/bots/daemon}`（本工作区的声明源在主仓 `bots/daemon/`，人格正文含内网约定 ⇒ 不在本仓）。修改纪律与生效路径 = 工作区的 `assistant/DISPATCH.md`「声明源与 spec 修改纪律」。
- `svc4web.py`：8080 实时端点，聚合四机 `svc.py status` 输出 markdown（dash.itab 消费）。
- `clean-make.py`：从干净环境执行 make 的薄壳（重启纪律 ②）——按 `agentd/envscrub.py` 名单洗刷调度身份变量后再跑 make，执行前自检、有残留即拒绝。子任务/服务进程内启停服务一律经它，不直接 `make`。
- `git-mirror-post-receive.sh`：dev 镜像回推 GitHub 的 post-receive hook（`make git-mirror.hooks` 安装）。

## 常用命令

```
make svc.status          # 全部服务状态 + 两类 drift（desired/actual + 版本 stale）
make svc.sync            # 按 env/services.yml 纠偏两类 drift：该起的起、该停的停，online 但版本 stale 的重启（stop+start；skip 行永不触碰）
make <name>.start/.stop  # 单服务启停（<name> ∈ env/services.yml）
make logs.trim           # 清理 run/logs（一律原地截断，不重启服务）
python3 svc/clean-make.py <name>.stop <name>.start   # 干净环境重启（任务/服务进程内必用；--dry-run 只看洗刷自检）
python3 svc/test_svc_path.py     # 单测：canonical_path() 的语义回归网（合成夹具，不启停服务）
python3 svc/test_git_sync.py     # 单测：git-sync.py 的拉取判定面（/tmp 沙箱，不写 ~/m）
# 工作区侧另有跨仓钉桩（不在本仓）：python3 ops/test_llm_router_accounts.py
```

## 指针

- 服务定义 schema 与全部名单：`env/services.yml`（头部注释是权威说明）
- 启停纪律、版本纪律、重启纪律：工作区根 `AGENTS.md`「服务与后台进程（make）」
  （按名引用形态 = `@ws-agents#behavior-rules`，只在工作区内可解）
- 服务排障流程：工作区 skill `service-troubleshooting`
- 单服务运维手册（**均在工作区主仓 `ops/`，不在本仓** —— 含内网端点/主机名/凭据面路径）：
  `ops/pi-web.md`、`ops/llm-router.md`、`ops/rsh.md`；对应服务的代码在各自独立仓
