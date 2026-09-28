# svc/ — 声明式服务生命周期层（declarative service lifecycle）

> **In English**: `svc.py` is the single execution layer for a declarative service
> registry (`services.yml`: `name` + `desired` + `version` + start definition). It
> implements start / stop / status / sync (reconcile) / log-trim, two independent
> drift judgements (desired-vs-actual and declared-version-vs-running-version), a
> channel-independent canonical `PATH` for service children, bounded liveness
> probes (process / HTTP / command-expect), and per-service pid+meta bookkeeping.
> Python 3 stdlib + PyYAML, POSIX shell for the Makefile shell. The rest of this
> file is the author's workspace manual (Chinese) and documents the exact
> semantics.

## 收录判据（本仓边界）

本仓**只住机制**：读一份声明式服务注册表、把现网纠偏到该声明的执行层，及其回归网。
**不住**（一律在它处，本仓不复述、只留指针）：

- **任何具体服务的定义与实现体**——启动定义（cmd/match/cwd/wrapper/status/env 钩子）住调用方工作区的注册表；服务的常驻监督壳（loop 脚本）、实现代码与单测住该服务自己的仓。
- **主机名、机器清单、仓名单、内网端点、凭据面路径**——凡「按部署面变化」的值都不进本仓代码（需要机器面/仓面时一律现场发现 ∨ 由调用方注入）。
- **单服务运维册与部署面事实**（含内网信息 ⇒ 不能公开）。

判据一句话：**改一处部署（加机器、换服务、改名单）不应产生本仓的 diff。**

## 是什么

- `svc.py`：服务生命周期唯一执行层。读调用方工作区的服务注册表（本工作区 = `env/services.yml`，单一事实源：name + desired + version + 启动定义），执行 start/stop/status/sync/trim。
  版本 stale = 运行 meta（`run/pids/<name>.meta`）**首行**≠ 声明 version（meta 缺失不算 stale），一律计 drift 并由 sync 重启。Makefile 的 `<name>.start/.stop/.status`、`svc.status`、`svc.sync`、`logs.trim` 都是它的薄壳。
  meta 形态：**首行 = 裸 version 数字**（stale 判定与一切「读 version 数字」的消费者——心跳、调度员、`cat`/`head -1`/`awk '{print $1}'`——只读这一行，形态不变），其后每行一个 `key=value` 的**运行态读数**（start 确认成功后写入）：`exe` = 该进程实际解析到的可执行文件（realpath）、`interp` = 它的 `--version` 首行。取不到一律写字面量 `unknown`（不留空、不伪造），三种成因：无 pid（`wrapper: none` 且 `match` 无命中）∨ 进程 non-dumpable（`sg`/`newgrp` 起的 ⇒ `/proc/<pid>/exe` 不可读）∨ exe 不在「可执行 `--version`」的解释器/外壳名单内（名单外的二进制一律不执行——观测不得启动语义未知的程序）。
  读数**只作观测**：`svc.status` 在摘要行后给 `--- runtime ---` 块（每个在线服务一行 exe + interp；旧格式 meta 只给一行计数）、`<name>.status` 给一行读数、`status --machine` 只加键（`exe`/`interp`/`exe_expected`/`exe_warn`）。与声明面不符时在 STATE 追加 `⚠ exe≠声明解析`，**但不计 drift、不进 stale、`svc.sync` 不因它重启任何服务**（drift 判据仍只有 desired/actual 与 version 两类）。声明面 = 按 `cmd` + 规范 PATH 解析内核本该 exec 到的文件（shebang 脚本再解一层解释器）；解析不出 ∨ 读数为 `unknown` ∨ meta 是旧格式 ⇒ 只显示、不判定。
  服务子进程的 `PATH` 与调用通道无关：内置前缀（存在的才进、去重，nvm 的 glob 命中按自然版本序降序全进 ⇒ 首个命中胜出）接继承的 PATH 尾 ⇒ **只加不减**，一份注册表在各机解析到同一解释器；继承 PATH 为空/缺失时补平台缺省地板（`os.defpath`），否则子进程连 shell 都解析不到。覆写口 `SVC_PATH_PREFIX`（冒号分隔，`~` 与 glob 均可）：非空 = **整体替换**内置前缀（不合并），空串 = 不加前缀。**整体替换 ⇒ 覆写值须自带服务孙进程所需的目录**（解释器的 `bin`、node 的 bin、平台包管理器的 bin）。start 的 spawn、`status.cmd` 探针与 `stop_cmd` 三处共用这一份构造。
  **服务的常驻壳不得自补 PATH**（`export PATH=…` 一类）：裸名解析只由这份规范 PATH 保证，壳内补段会让「以哪个解释器起来了」重新取决于载体（缺组自愈的 `sg … -c` re-exec、开机自启等），且删补段时的复发条件与实体依赖 = `lore/library/agentfw/facts/machine-toolchain.md`「删掉 loop 脚本的 PATH 补段后…」条。
- `test_svc_path.py`：`canonical_path()` 的语义回归网（合成夹具，不启停服务）。

## 常用命令

```
make svc.status          # 全部服务状态 + 两类 drift（desired/actual + 版本 stale）
make svc.sync            # 按注册表纠偏两类 drift：该起的起、该停的停，online 但版本 stale 的重启（stop+start；skip 行永不触碰）
make <name>.start/.stop  # 单服务启停（<name> ∈ 注册表）
make logs.trim           # 清理 run/logs（一律原地截断，不重启服务）
python3 svc/test_svc_path.py     # 单测
```

（以上是调用方工作区的入口形态；本仓自身不含 Makefile 与服务定义。）

## 指针

- 服务定义 schema 与全部名单：调用方工作区的注册表（本工作区 = `env/services.yml`，头部注释是权威说明）
- 启停纪律、版本纪律、重启纪律：工作区根 `AGENTS.md`「服务与后台进程（make）」
  （按名引用形态 = `@ws-agents#behavior-rules`，只在工作区内可解）
- 服务排障流程：工作区 skill `service-troubleshooting`
- 各服务的常驻壳、实现体与跨机状态视图：均不在本仓（住各自服务仓与调用方工作区；本仓只从注册表读到它们的启动命令）
- 单服务运维册与部署面事实（内网端点/主机名/凭据面路径 ⇒ 不能公开）：住调用方工作区的运维册目录（本工作区 = `ops/`，收录判据 = 其 README）
- 跨仓依赖一枚：身份洗刷名单的单点 = `agentd/envscrub.py`（与服务监督侧 runner spawn 同口径，禁两份名单漂移）
