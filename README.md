# serviced/ — 声明式服务生命周期层

## 收录判据（本仓边界）

本仓**只住机制**：读一份声明式服务注册表、把现网纠偏到该声明的执行层，驱动它的常驻纠偏壳，及其回归网。
**不住**（一律在它处，本仓不复述、只留指针）：

- **任何具体服务的定义与实现体**——注册表（profile 与逐机声明）住调用方工作区；服务的常驻监督壳（loop 脚本）、实现代码与单测住该服务自己的仓。（本条的「服务」= **被纠偏的**那些；纠偏层自己的常驻壳（`sync-loop.sh`）不含任何部署面值 ⇒ 是机制、住本仓。）
- **主机名、机器清单、仓名单、内网端点、凭据面路径**——凡「按部署面变化」的值都不进本仓代码（需要机器面/仓面时一律现场发现 ∨ 由调用方注入 ∨ 走环境变量覆写口）。
- **单服务运维册与部署面事实**（含内网信息 ⇒ 不能公开）。

判据一句话：**改一处部署（加机器、换服务、改名单、改期望态）不应产生本仓的 diff。**

## 是什么

- `serviced.py`：服务生命周期唯一执行层（读两层 JSON 注册表 → start/stop/status/sync/trim）。**机制语义的单一事实源 = 它的模块 docstring**（注册表形态与本机面、全部子命令、Pointers、Invariants：drift 判据、规范 PATH、meta 形态与运行态读数、pid 复用护栏、trim 语义等）+ 各函数 docstring 与行内注释 ⇒ 本 README 不复述、只留下面两条入口性事实。
- `sync-loop.sh`：常驻纠偏循环——每 `SERVICED_SYNC_INTERVAL` 秒（缺省 60）跑一次 `serviced.py sync`，由调用方以一枚服务声明跑它（服务名与逐机期望态不在本仓）。为何是「壳 + 独立进程组的子进程」而不是给 sync 加一个 `--loop`（sync 会把自己按 stale 停掉）、停它自身的语义、日志与静默口径 = 其头注。
- `test_serviced_path.py` / `test_registry.py`：`canonical_path()` 与两层注册表 loader 的语义回归网（合成夹具，不启停服务；后者含 config error 全枚举与本机空面告警）。
- 零第三方依赖（Python 3 标准库）。

入口性事实（调用方最常踩的两条，权威仍在代码内）：注册表的任何解析/校验失败一律 config error 退出、不静默跳过（一份解析不了的声明不能从它治理的面上消失）；**本机零声明不是错误但只给 stderr 一行告警**，此时 `status` 会打出健康的 `0 drift` ⇒ 判健康先看服务计数。

## 常用命令

```
make serviced.status     # 本机面：全部服务状态 + drift
make serviced.sync       # 按注册表纠偏：该起的起、该停的停、版本 stale 的重启
make <name>.start/.stop  # 单服务启停（<name> ∈ services/profiles/）
make logs.trim           # 清理 run/logs（一律原地截断，不重启服务）
make <常驻纠偏服务名>.start  # 起 sync-loop.sh（服务名由调用方声明；本工作区 = service-sync）
python3 serviced/test_serviced_path.py   # 单测：canonical_path()
python3 serviced/test_registry.py        # 单测：注册表 loader
```

（以上是调用方工作区的入口形态；本仓自身不含 Makefile 与任何服务定义。）

## 指针

- 注册表 schema、字段语义与全部名单：调用方工作区的注册表（本工作区 = `services/README.md`）
- 机制语义与不变量：`serviced.py` 模块 docstring（Invariants）
- 启停纪律、版本纪律、重启纪律：调用方工作区的运维政策（本工作区 = `services/README.md`「启停与重启纪律」与「版本纪律（硬约束）」两节）
  （按名引用形态 = `@ws-agents#behavior-rules`，只在工作区内可解）
- 各**被纠偏**服务的常驻壳、实现体与跨机状态视图：均不在本仓（住各自服务仓与调用方工作区；本仓只从注册表读到它们的启动命令）
- 单服务运维册与部署面事实（内网端点/主机名/凭据面路径 ⇒ 不能公开）：住调用方工作区的运维册目录（本工作区 = `ops/`，收录判据 = 其 README）
- 跨仓依赖两枚：身份洗刷名单的单点 = `agentd/envscrub.py`（与服务监督侧 runner spawn 同口径，禁两份名单漂移）；`env_file` 解密 = `encrypt/envdec.py`
