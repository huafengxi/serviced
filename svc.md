<!--
首块 = rsh 通道（单次 `rsh -l`，系统健康）；其余每 host 一个 widget：单独 run/refresh 互不牵连；页面带 _v=autorun 打开自动执行。各 host 另有「sync」手动按钮（感叹号前缀语法的手动 widget）：跑该机 `svc/svc.py sync` 纠偏两类 drift（desired/actual 启停 + 在线服务版本 stale 的 stop+start 重启；skip 行永不触碰）——变更操作，不参与 autorun/run-all，仅手点。widget 不再串接状态回显；有动作时 sync 自身末尾打一次纯文本状态（`--- after sync ---`），无 drift 时只打 `no drift, nothing to do`。sync widget 显式带 `-t 600`：重型服务（maas 类 `start_timeout: 180` + 探针 30s）stop+start 一轮远超 rsh 缺省 60s，缺省下 rsh 会先杀远端命令、留下半程 sync 的观感；只读 status widget 不加（`svc4web.py` 自带 18s 上限口径）。
env/PATH 口径（服务子进程的规范 PATH、SVC_PATH_PREFIX 覆写口）= svc/README.md 的 svc.py 条目；本页是 dash 视图（消费方 dash/dash.itab 的 autorun widget），不承载机制叙述。
-->

## rsh 通道

${svc/svc4web.py rsh-online}

## dev

${rsh/rsh dev -- svc/svc4web.py dev}

${!rsh/rsh dev -t 600 -- svc/svc.py sync}

## mac

${rsh/rsh mac -- svc/svc4web.py mac}

${!rsh/rsh mac -t 600 -- svc/svc.py sync}

## nv1

${rsh/rsh nv1 -- svc/svc4web.py nv1}

${!rsh/rsh nv1 -t 600 -- svc/svc.py sync}

## nv2

${rsh/rsh nv2 -- svc/svc4web.py nv2}

${!rsh/rsh nv2 -t 600 -- svc/svc.py sync}
