#!/usr/bin/env python3
"""svc.py — reconcile services against env/services.yml (single source of
truth) and execute the service lifecycle.

Usage:
  svc.py status            desired-vs-actual table + drift count + runtime
                           block (the exe/interpreter each online service
                           actually resolved to), all read-only
  svc.py status --machine  the same data as one JSON line {host, total, drift,
                           services:[{name, version, run_version, stale,
                           desired, actual, state}]} — svc4web.py's input
  svc.py sync              reconcile both drift kinds: start desired-online
                           that is offline, stop desired-offline that is
                           online, restart (stop+start) online-but-stale
  svc.py trim              truncate every run/logs/*.log in place, no restart
  svc.py start NAME        start per the entry's lifecycle fields (cmd/match/
                           stop_match/cwd/wrapper/stop_cmd/status/env hooks)
  svc.py stop NAME         stop_cmd when declared, else TERM then KILL the
                           pidfile process (its whole group) + pattern hits;
                           removes pidfile and version meta
  svc.py status NAME       one liveness probe (proc | http | cmd/expect);
                           exit 0 = online, non-zero = offline

Pointers — single sources, not restated here:
  schema and field semantics    the service registry file's own header comment
                                (this workspace: env/services.yml)
  start/stop, version, restart  the deployment's operating rules (this workspace:
                                root AGENTS.md「服务与后台进程（make）」)
  troubleshooting               the deployment's service-troubleshooting playbook
  incidents behind the rules    the deployment's incident notes — e.g. why a
      `pgrep -f` self-check pattern must not match itself, why processes started
      via `sg`/`newgrp` are non-dumpable (so `lsof` and `/proc/<pid>/fd` are
      blind to them), and the three iron rules of a cross-machine restart window

Invariants (rule bodies):
  1. Pattern matching excludes this process and its wrapper ancestors, walking
     up only while an ancestor is a wrapper, so real daemons stay matchable
     (self_and_ancestors()); declared patterns are anchored (^…$).
  2. A stop target that is a process-group leader is signalled with its group.
  3. Pidfile liveness requires a matching recorded lstart (pid-reuse guard); a
     live pid whose lstart is unreadable trusts the pidfile (accepted residual
     risk = a reuse inside that window).
  4. `trim` truncates in place and never unlinks: holder detection via lsof /
     /proc/<pid>/fd is blind for non-dumpable processes.
  5. Start order: read declared version → launch → write run/pids/NAME.meta
     only after the start is confirmed (spawn / status probe).
  6. A missing meta renders `运行 v?` and is never stale; skip rows are probed
     for display only — never drift, never stale, never touched by sync.
  7. Version drift does not distinguish the change face: online + meta present
     + meta != declared version ⇒ stale, counted in the drift figure and
     restarted by sync (item 6's missing-meta rule still holds).
  8. desired=offline means stop, never restart; human and --machine status
     share one wording source (_status_fields).
  9. Service children get a channel-independent PATH: clean_env() replaces the
     inherited PATH with PATH_PREFIX (existing dirs only, deduped) followed by
     the inherited tail, so one services.yml resolves to one interpreter no
     matter which channel ran make; SVC_PATH_PREFIX overrides the prefix
     wholesale (empty string = no prefix = the pre-normalization behaviour).
     With nothing inherited, os.defpath supplies the system floor: a PATH
     without /bin would not even resolve the shell that `cmd` entries run.
 10. The runtime reading (exe/interp) is observation only: recorded in the meta
     at start, shown by `status`, never counted as drift, never making a row
     stale, never restarting anything. Anything unobtainable degrades to the
     literal `unknown` — a reading cannot fail a start.
 11. The meta's FIRST line stays the bare declared version (its pre-runtime
     shape), so every reader that takes the version number from that file is
     unaffected by the `key=value` runtime lines that follow it.
"""
import glob
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS / "agentd"))
sys.path.insert(0, str(WS / "encrypt"))
import envdec  # noqa: E402  env_file 解密 + KV 正则单点
import envscrub  # noqa: E402  spawn 环境洗刷名单单点
SVC_FILE = WS / "env" / "services.yml"
LOGS_DIR = WS / "run" / "logs"
PIDS_DIR = WS / "run" / "pids"
# Cap on the `status.cmd` probe subprocess itself (probe_online): a probe that
# runs longer is reported offline instead of holding the status table.
# Every wait in svc.py is bounded by its own timeout — gather() joins its probe
# threads without a per-future cap, so no probe may be unbounded.
PROBE_TIMEOUT = 30
# Cap on the two `ps` scans (ps_table / pid_lstart): a hung `ps` (D state,
# stuck procfs) yields an empty table / an empty lstart instead of stalling
# status or stop. An empty lstart lands in pidfile_alive's trust-the-pidfile
# fallback — on every platform, not only where `ps` enumeration is blocked.
PS_TIMEOUT = 10
# Cap on the ONE subprocess the runtime reading spawns (`<exe> --version` at
# start): an interpreter that hangs on it yields `unknown`, never a stuck start.
VERSION_TIMEOUT = 5
# The meta is line-oriented (`key=value`), so a reading is one line: whitespace
# collapsed, then cut here.
VERSION_LEN = 120
# Written instead of an unobtainable reading — explicit, never empty, never
# invented (a blank field reads as a bug, `unknown` reads as a fact).
UNKNOWN = "unknown"
# Executables whose `--version` we run. Observation must never execute a binary
# with unknown semantics (an undefined flag could START it, or make it wait on
# stdin), so the reading is limited to interpreters/shells — prefix/glob shaped
# on purpose: a realpath carries the version suffix (conda's `python3` is
# `python3.13`). Anything else records `unknown` (current instance: clash's
# `mihomo`).
EXE_VERSION_ALLOW = re.compile(r"python[0-9.]*|node|nodejs|bash|sh|dash|zsh|ksh")

# 规范 PATH（通道无关）：服务子进程解析到哪个解释器不得取决于「谁调用
# 了 make」。实测缺陷（dev）：登录 shell 与会话通道的 PATH 都不含 ~/miniconda3/bin
# ⇒ 裸 `python3`（rshd/rsh-worker 的 cmd）与 `#!/usr/bin/env python3` shebang 脚本
# （git-sync/dav-sync/convert-file/maas）落到 /usr/bin/python3.8，而 nv1/nv2/mac 的
# 通道解析到 conda 3.14/3.11 ⇒ 同一份 services.yml、不同解释器（工作区约定 = Python
# 一律 ~/miniconda3，根 AGENTS.md「运行环境」）。
# 前缀只收**有点名现网消费者**的目录（逐段见行内注释）：存在的才进、去重，随后接
# 继承的 PATH ⇒ 只加不减，不在前缀里的目录照旧可见（删段永不构成回退）。顺序按各机
# 登录 shell 的既有优先级排：node 目录在 /usr/local/bin 之前 ⇒ dev 的 `pi` 仍解析到
# nvm 那支，不会换成 /usr/local/bin 里的另一份全局安装。
# 覆写口 SVC_PATH_PREFIX（冒号分隔，`~` 与 glob 均可）：设了即**整体替换**内置前缀
# （不合并——合并表达不出「去掉某段」）；设为空串 = 不加前缀 = 改动前行为（回退无需
# revert 代码）。
PATH_PREFIX = (
    # python3：git-sync（四机）、rshd（dev）、rsh-worker（四机）、dav-sync /
    # shared-watch / qwen3 / krea2（nv1）；工作区约定的解释器
    "~/miniconda3/bin",
    # node/pi/pi-web/cc-connect：nv1、nv2 只在这里。消费者 = agentd 的孙进程
    # （pi-wrap/pi-rpc-wrap.py 用裸 "pi"）与 web 的会话宿主解析
    # （w/ext/sessiond/proc.py 的 shutil.which("pi")）
    "~/.local/node/bin",
    # 同上：dev——dev 唯一的 node 安装（/usr/local/bin 只有 pi 符号链接、没有 node，
    # 而 pi 的 shebang 是 env node）。多版本并存时按自然版本序降序全进，首个命中胜出
    "~/.nvm/versions/node/*/bin",
    # 同上：mac（node/pi-web/git 只在 homebrew）
    "/opt/homebrew/bin",
    # mihomo：clash 服务（dev；nv1 的同名二进制亦在此）
    "/usr/local/bin",
)


def _version_key(path):
    """Natural sort key: digit runs compare as numbers, so v22 > v8 (nvm keeps
    old versions side by side and the first PATH hit wins — the pick must be
    deterministic, not lexicographic)."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", path)]


def _prefix_dirs(spec):
    """One PATH_PREFIX entry → existing directories (`~` expanded, `*` globbed,
    glob hits in natural-version order, newest first)."""
    p = os.path.expanduser(spec)
    hits = glob.glob(p) if "*" in p else [p]
    return [d for d in sorted(hits, key=_version_key, reverse=True)
            if os.path.isdir(d)]


def canonical_path(inherited=None):
    """Channel-independent PATH for service children: the built-in prefix (or
    the SVC_PATH_PREFIX override) followed by the inherited PATH — `~`/glob
    expanded, existing directories only, first occurrence wins, empty entries
    dropped (an empty PATH entry means the cwd: an accident, never an intent).
    `inherited` defaults to this process's own PATH. When nothing is
    inherited, os.defpath (the platform's own default search path) supplies the
    system floor — clean_env() always writes a PATH, so neither Python's
    os.get_exec_path() CS_PATH fallback nor the child shell's default would
    otherwise apply, and `cmd: [bash, …]` entries could not even start."""
    override = os.environ.get("SVC_PATH_PREFIX")
    specs = override.split(os.pathsep) if override is not None else PATH_PREFIX
    out = []
    for spec in specs:
        for d in _prefix_dirs(spec):
            if d not in out:
                out.append(d)
    src = os.environ.get("PATH", "") if inherited is None else inherited
    for d in src.split(os.pathsep):
        if d and d not in out:
            out.append(d)
    if not src.strip(os.pathsep):
        # The prefix alone has no system directory, so a child would get a PATH
        # that cannot resolve `sh`/`bash`/`git`. Only reached when the caller
        # contributed nothing (an empty or missing inherited PATH); with any
        # inherited entry the output is unchanged.
        out.extend(d for d in os.defpath.split(os.pathsep) if d and d not in out)
    return os.pathsep.join(out)


# 洗刷名单单点 = agentd/envscrub.py（runner spawn 同口径，禁两份名单漂移）：
# a service restarted from inside a dispatched task must not inherit the
# task's identity.


def clean_env():
    # No keep set: the secrets a service needs are injected AFTER the scrub
    # from the entry's env_file decryption, so nothing must survive it.
    env = envscrub.scrub_env(keep=frozenset())
    # envscrub's lists never mention PATH, so the calling channel's PATH used to
    # reach service children verbatim (and decided which python3/node they got);
    # replace it with the channel-independent form (prefix + inherited tail).
    # Covers both callers: build_env (start spawn) and stop_cmd (maas.py stop).
    env["PATH"] = canonical_path(env.get("PATH", ""))
    return env


def load_services():
    with open(SVC_FILE) as f:
        data = yaml.safe_load(f)
    svcs = data["services"]
    names = [s["name"] for s in svcs]
    if len(names) != len(set(names)):
        sys.exit(f"error: duplicate service names in {SVC_FILE}")
    return svcs


def _host_matches(hostnames, entries):
    """Case-insensitive host match: an entry matches if it equals the
    hostname or is a dot-separated prefix of it (e.g. `nv1` matches
    hostnames `nv1` and `nv1.example.com`)."""
    hn = hostnames.lower()
    return any(hn == h or hn.startswith(h + ".")
               for h in (str(x).lower() for x in entries))


def applies_here(svc):
    """Whether this service's `desired` applies on this machine: `hosts`
    restricts, `exclude_hosts` excludes, the two are mutually exclusive (both
    present = config error, exit). applies = (no hosts or hosts match) AND (no
    exclude_hosts or exclude_hosts do NOT match)."""
    hosts = svc.get("hosts")
    excluded = svc.get("exclude_hosts")
    if hosts and excluded:
        sys.exit(f"error: service {svc.get('name')!r} has both 'hosts' and "
                 f"'exclude_hosts' in {SVC_FILE.name} — they are mutually "
                 "exclusive; pick one")
    hn = platform.node().lower()
    if hosts and not _host_matches(hn, hosts):
        return False
    if excluded and _host_matches(hn, excluded):
        return False
    return True


def _probe_actual(svc):
    """'online' or 'offline' — the in-process equivalent of `svc.py status
    <name>`; a `_validate_entry` failure (sys.exit) counts offline, the verdict
    a non-zero probe exit would give."""
    try:
        _validate_entry(svc)
    except SystemExit as e:
        print(f"warning: {svc.get('name')}: invalid entry ({e}); "
              "treated as offline", file=sys.stderr)
        return "offline"
    ok, _ = probe_online(svc)
    return "online" if ok else "offline"


def gather():
    """[(svc, actual, drift, applies)] for every declared service, in
    load_services() order; probes run in-process and in parallel.

    Skip rows are probed too (the svc tab shows them when running) but their
    drift is hardwired False, and their probe is pidfile-only — deliberately
    NOT the full probe: a status target that no-ops to exit 0 where the
    service is n/a would falsely report online.

    Each probe is bounded by its own timeout (http: urlopen; cmd:
    PROBE_TIMEOUT; proc: PS_TIMEOUT), which is what makes the plain `with`
    join safe; a per-future cap would only serialize the wait (N hung probes
    ⇒ ~N×cap). Unexpected errors count offline with a stderr warning."""
    svcs = load_services()
    applies = [applies_here(s) for s in svcs]  # once per service, reused below
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [
            ex.submit(_probe_actual, s) if a else ex.submit(
                lambda s=s: "online" if pidfile_alive(s["name"]) else "offline")
            for s, a in zip(svcs, applies)]
        rows = []
        for svc, f, a in zip(svcs, futures, applies):
            try:
                actual = f.result()
            except Exception as e:
                print(f"warning: {svc['name']}: probe error ({e!r}); "
                      "treated as offline", file=sys.stderr)
                actual = "offline"
            rows.append((svc, actual, a and actual != svc["desired"], a))
    return rows


def _version_fields(svc, actual, applies):
    """(run_version, note, stale) for one row; meaningful only while online.
    run_version = the meta's recorded version, '?' when the meta is missing;
    note = the STATE annotation ('' when versions match).
    stale = True iff online AND meta present AND meta != declared: the change
    face is not distinguished, so any bump whose restart has not landed on this
    host reads as stale. A missing meta is never stale (no false positives);
    skip rows annotate but never count as drift. The drift figure, the
    `--machine` payload and sync's restart branch all consume this one return
    value."""
    if actual != "online":
        return None, "", False
    declared = str(svc.get("version", "-"))
    run_v = read_meta(svc["name"])
    if run_v is None:
        return "?", f"（运行 v?，代码 v{declared}）", False
    if run_v != declared:
        return run_v, f"stale（运行 v{run_v}，代码 v{declared}，需重启）", applies
    return run_v, "", False


def _status_fields(svc, actual, drift, applies):
    """(actual_str, state_str, stale) for one row; shared by the human and
    machine outputs so their wording cannot diverge. `stale` = version drift,
    separate from desired-vs-actual drift but counted in the drift figure."""
    run_v, vnote, stale = _version_fields(svc, actual, applies)
    if not applies:
        if svc.get("hosts"):
            limit = f"hosts: {', '.join(map(str, svc['hosts']))}"
        else:
            limit = f"exclude_hosts: {', '.join(map(str, svc.get('exclude_hosts') or []))}"
        state = f"skip ({limit})"
        if actual == "online":
            # not managed on this host, but actually running — flag it so the
            # svc tab shows the row (display rule: actual OR desired online)
            state += " [online]"
            if vnote:
                state += f" {vnote}"
        return actual, state, False
    if drift:
        state = f"DRIFT (want {svc['desired']}, is {actual})"
        if vnote:
            state += f" {vnote}"
        return actual, state, stale
    state = "ok"
    if vnote:
        state = vnote
    return actual, state, stale


def _runtime_fields(svc, actual):
    """(exe, interp, expected, warn) for one row — the recorded runtime reading
    plus the declaration-side resolution it is compared against.

    Observation only (invariant 10): `warn` never feeds the drift figure, never
    makes a row stale, never restarts anything. It is non-empty only when BOTH
    sides are known and differ; an `unknown` reading (no pid, a non-dumpable
    process, a binary outside EXE_VERSION_ALLOW), an unresolvable declaration,
    or an old-format meta (no runtime lines) leaves nothing to compare ⇒ display
    without a verdict. exe is None for offline rows and old-format metas."""
    if actual != "online":
        return None, None, None, ""
    fields = read_meta_fields(svc["name"])
    exe = fields.get("exe")
    if not exe:
        return None, None, None, ""   # meta written before the runtime fields
    interp = fields.get("interp") or UNKNOWN
    expected = expected_exe(svc)
    warn = ""
    if exe != UNKNOWN and expected and os.path.realpath(exe) != expected:
        warn = (f" ⚠ exe≠声明解析（运行 {exe}，声明解析 {expected}；"
                "不计 drift，下次重启自然对齐）")
    return exe, interp, expected, warn


def _print_runtime(runtime, no_reading, w):
    """The block after the summary: what each online service's process actually
    resolved to at its last start (invisible before this face existed: a service
    running under the wrong interpreter looked exactly like a healthy one)."""
    if not runtime and not no_reading:
        return
    print("\n--- runtime（启动时记录的实际 exe / 解释器；unknown = 取不到）---")
    for name, exe, interp in runtime:
        print(f"{name:<{w}}  {exe}  {interp}")
    if no_reading:
        print(f"（另有 {no_reading} 枚在线服务的 meta 是旧格式、无读数；"
              "下次重启后自动补全）")


def cmd_status():
    rows = gather()
    w = max(len(s["name"]) for s, _, _, _ in rows)
    wv = max(max(len(str(s.get("version", "-"))) for s, _, _, _ in rows),
             len("VERSION"))
    print(f"{'SERVICE':<{w}}  {'VERSION':<{wv}} {'DESIRED':<8} {'ACTUAL':<8} STATE")
    print(f"{'-'*w}  {'-'*wv} {'-'*8} {'-'*8} -----")
    n_drift = 0
    runtime, no_reading = [], 0
    for svc, actual, drift, applies in rows:
        actual_s, state, stale = _status_fields(svc, actual, drift, applies)
        exe, interp, _, warn = _runtime_fields(svc, actual)
        n_drift += 1 if (drift or stale) else 0
        ver = str(svc.get("version", "-"))
        print(f"{svc['name']:<{w}}  {ver:<{wv}} {svc['desired']:<8} "
              f"{actual_s:<8} {state}{warn}")
        if actual == "online":
            if exe is None:
                no_reading += 1
            else:
                runtime.append((svc["name"], exe, interp))
    print(f"\n{len(rows)} services, {n_drift} drift")
    _print_runtime(runtime, no_reading, w)
    return 0


def cmd_status_machine():
    """Machine-readable status: one JSON object on one line (consumed by
    svc4web.py multi-host aggregation)."""
    rows = gather()
    services = []
    n_drift = 0
    for svc, actual, drift, applies in rows:
        actual_s, state, stale = _status_fields(svc, actual, drift, applies)
        exe, interp, expected, warn = _runtime_fields(svc, actual)
        n_drift += 1 if (drift or stale) else 0
        # additive keys only: consumers render the five original ones
        services.append({"exe": exe, "interp": interp,
                         "exe_expected": expected, "exe_warn": bool(warn),
                         "name": svc["name"],
                         "version": str(svc.get("version", "-")),
                         "run_version": read_meta(svc["name"]) or "?"
                         if actual == "online" else None,
                         "stale": stale,
                         "desired": svc["desired"],
                         "actual": actual_s,
                         "state": state})
    print(json.dumps({"host": platform.node(), "total": len(rows),
                      "drift": n_drift, "services": services},
                     ensure_ascii=False))
    return 0


def _run_action(name, act):
    """(rc, reason) for one in-process start/stop action; never raises. A
    failing action must not abort the remaining ones — the Makefile's `%.stop`
    recipe carries a `-` prefix for exactly that reason."""
    try:
        rc = (cmd_start_name if act == "start" else cmd_stop_name)([name])
    except SystemExit as e:       # config / require_env errors exit with a msg
        return 1, str(e.code)
    except Exception as e:        # e.g. cmd not executable
        return 1, repr(e)
    return (rc or 0), f"exit {rc}"


def cmd_sync():
    """Reconcile both drift kinds:
    ① desired-vs-actual — start what should be online but is offline, stop
       what should be offline but is online (never a restart);
    ② version staleness — online + desired=online + running version (meta)
       != declared version → restart (stop, then start).
    Staleness comes from `_version_fields`, so its invariants hold here: skip
    rows and rows with a missing meta are never stale and so are never
    touched. Rows act in services.yml order; the start/stop calls run
    in-process (`_run_action`) and one failing action never aborts the rest."""
    rows = gather()
    actions = []  # [(action line, name, [act, ...])], act ∈ (start, stop)
    for svc, actual, drift, applies in rows:
        name = svc["name"]
        if drift:
            want = svc["desired"]
            actions.append((f"[sync] {name}: desired={want} actual={actual}",
                            name, ["start" if want == "online" else "stop"]))
            continue  # desired=offline+online → stop only, never a restart
        _, vnote, stale = _version_fields(svc, actual, applies)
        if stale and svc["desired"] == "online":
            actions.append((f"[sync] {name}: {vnote}, restarting",
                            name, ["stop", "start"]))
    if not actions:
        print("no drift, nothing to do")
        return 0
    for line, name, acts in actions:
        # flush: our stdout is block-buffered when piped (rsh / dash widget),
        # so the action line must not land after the start/stop output.
        print(line, flush=True)
        for act in acts:
            rc, why = _run_action(name, act)
            if rc:
                print(f"  !! {name}.{act} failed ({why})")
    # re-check
    print("\n--- after sync ---")
    return cmd_status()


def human_iec(n):
    """Human-readable size in IEC units, ~`numfmt --to=iec` (e.g. 1536 -> 1.5K)."""
    if n < 1024:
        return str(n)
    for i, u in enumerate("KMGTPE"):
        if n < 1024 ** (i + 2) or u == "E":
            return f"{n / 1024 ** (i + 1):.1f}{u}"


def cmd_trim():
    """Truncate every run/logs/*.log in place — no liveness probe, no unlink.

    `os.truncate(f, 0)` frees the space whether or not a process holds the
    file and keeps the inode/fd valid for live holders (they keep writing at
    their old offset; the sparse NUL hole is cosmetic). Holder detection must
    not come back: `sg`/`newgrp`-started processes are non-dumpable, so lsof
    and /proc/<pid>/fd are blind even to their owner and a LIVE log looks
    orphaned — unlinking it leaves the service writing into a deleted inode
    (lore §「`sg`/`newgrp` 起的进程是 non-dumpable ⇒ `lsof` 与
    `/proc/<pid>/fd` 对它全盲」). Cost accepted: retired services leave
    0-byte logs. Trim destroys evidence ⇒ it comes AFTER any log forensics
    (lore §「跨机重启窗口 runbook 三铁律」; paths in the module Pointers)."""
    trimmed = freed = 0
    for f in sorted((WS / "run" / "logs").glob("*.log")):
        if not f.is_file():
            continue
        sz = f.stat().st_size
        os.truncate(f, 0)  # in place: keeps inode/fd valid for any holder
        print(f"{'trimmed':<10}{f.relative_to(WS)} ({sz} bytes)")
        trimmed += 1
        freed += sz
    print(f"summary: {trimmed} trimmed, "
          f"freed {freed} bytes ({human_iec(freed)})")
    return 0


# --------------------------------------------------------------------------
# lifecycle execution layer (start/stop/status NAME ...)
# --------------------------------------------------------------------------

def ps_table():
    """[(pid, ppid, command)] for all processes (macOS + Linux, no /proc).
    A `ps` that exceeds PS_TIMEOUT yields an empty table: pattern matching
    then matches nothing and the exclusion walk covers only this process."""
    try:
        r = subprocess.run(["ps", "-ww", "-eo", "pid=,ppid=,command="],
                           stdout=subprocess.PIPE, text=True,
                           timeout=PS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return []
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


# Command-line shapes of the invocation/wrapper chain above svc.py: they only
# carry our own argv text (or are pure launchers), so they must never be
# matched/killed. The upward walk stops at the first ancestor matching NONE.
_WRAPPER_CHAIN_RES = (
    re.compile(r"\bsvc\.py\b"),            # parent svc.py (sync -> make -> svc.py)
    re.compile(r"(?:\A|/)g?make(?:\s|\Z)"),            # make running a recipe
    re.compile(r"(?:\A|/)(?:ba|da|z|k|fi)?sh\s+(?:-\S+\s+)*-\S*c"),
    # ^ sh -c / bash -lc recipe shells AND caller tool shells (`bash -c '<whole
    #   command line>'`); a login shell (`-bash`) and real daemons
    #   (`bash svc/agentd-loop.sh`) do not match.
    re.compile(r"(?:\A|/)(?:sg|newgrp)(?:\s|\Z)"),     # supplementary-group switch
    re.compile(r"(?:\A|/)(?:sudo|env|timeout|setsid|nohup)(?:\s|\Z)"),
)


def _is_wrapper(cmdline):
    """True if the cmdline is a launcher/wrapper shape (see _WRAPPER_CHAIN_RES)."""
    return any(r.search(cmdline) for r in _WRAPPER_CHAIN_RES)


def self_and_ancestors(table):
    """Pids to exclude from pattern matching: this process plus the WRAPPER
    ancestors above it (`table` = the caller's ps_table() snapshot). Walk up
    while the ancestor's cmdline is a launcher/wrapper shape (`_is_wrapper`)
    and STOP at the first that is not — that one is a real process (daemon /
    session carrier) and must stay matchable.

    Both ends are load-bearing:
      ① excluding the chain keeps a caller whose own cmdline merely mentions a
         match/stop_match pattern (one shell line holding both a stop and a
         `pgrep -f "<that pattern>"`) from being SIGTERMed by its own stop —
         the stop lands, the start after it never runs, nothing prints and the
         service stays offline. Judging the chain by "cmdline contains
         'svc.py'" does NOT work: through a launcher above make that token sits
         in the make recipe shell, not in the caller's cmdline.
      ② stopping at the first non-wrapper keeps a supervised restart possible:
         agentd-loop.sh -> runner.py -> pi-rpc-wrap.py -> pi -> (tool shell) ->
         make -> sh -c -> svc.py stops at the pi session, so
         `bash svc/agentd-loop\\.sh` above it stays matchable. Daemons we start
         are detached (start_new_session) and are never ancestors at all.
    Residual disciplines (new launcher shapes must be added to
    _WRAPPER_CHAIN_RES; an unanchored stop_match can still hit third parties
    outside the chain): lore §「`pgrep -f` 自证时 pattern 必须防自匹配」.
    """
    ppid = {pid: pp for pid, pp, _ in table}
    cmd = {pid: c for pid, _, c in table}
    out, pid = {os.getpid()}, os.getpid()
    while pid:
        pid = ppid.get(pid, 0)
        if not pid or not _is_wrapper(cmd.get(pid, "")):
            break  # first non-wrapper ancestor: real process, keep matchable
        out.add(pid)
    return out


def matched_pids(patterns):
    """Pids whose command line matches any pattern (re.search), excluding
    ourselves and ancestors (self-match protection). One ps snapshot serves
    both the exclusion walk and the match."""
    if not patterns:
        return []
    table = ps_table()
    excl = self_and_ancestors(table)
    return [pid for pid, _, cmd in table
            if pid not in excl
            and any(re.search(p, cmd) for p in patterns)]


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_lstart(pid):
    """Process start time string (precise pid-reuse guard; identity via
    cmdline is unreliable: e.g. `bash -c '<one cmd>'` exec-replaces itself).
    "" when unreadable — including a `ps` that exceeds PS_TIMEOUT, which is
    what pidfile_alive's trust-the-pidfile fallback keys on."""
    try:
        r = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "lstart="],
                           stdout=subprocess.PIPE, text=True,
                           timeout=PS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout.strip()


def pidfile(name):
    return PIDS_DIR / f"{name}.pid"


def metafile(name):
    """Version meta, written by `start NAME` after launch confirmation:
    line 1 = the services.yml version in effect for the running process (its
    original shape, invariant 11), followed by `key=value` runtime lines
    (`exe`, `interp`) describing what that process actually resolved to."""
    return PIDS_DIR / f"{name}.meta"


def read_meta(name):
    """Recorded running version = the meta's FIRST line, or None if the meta is
    missing/unreadable (legacy process, or never started via svc.py start).
    Only the first line is read, so the runtime lines below it cannot reach the
    version-drift check."""
    try:
        v = metafile(name).read_text().split("\n", 1)[0].strip()
        return v or None
    except OSError:
        return None


def read_meta_fields(name):
    """{key: value} of the meta's `key=value` lines — {} for a missing meta and
    for an old-format one (written before the runtime fields existed), which is
    how `_runtime_fields` tells "no reading" from "reading = unknown"."""
    try:
        lines = metafile(name).read_text().split("\n")[1:]
    except OSError:
        return {}
    fields = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key:
            fields[key] = value
    return fields


def read_pidfile(name):
    """(pid, lstart) or None; tolerates missing/corrupt/legacy files."""
    try:
        parts = pidfile(name).read_text().split("\t")
        return int(parts[0]), parts[1].strip()
    except (OSError, ValueError, IndexError):
        return None


def pidfile_alive(name):
    """Pid from pidfile is alive AND has the recorded start time (pid-reuse
    guard). Fallback: endpoint security can hide a LIVE process from `ps`
    enumeration inside an agent task's process tree, so pid_lstart() returns
    "" and the strict match would judge the service offline — false `DRIFT`
    and, worse, a failed start dedup that double-launches the daemon. A live
    pid (os.kill(pid, 0) is not blocked) with an unreadable lstart therefore
    trusts the pidfile; accepted residual risk = a reuse inside that window,
    far less harmful than a double launch. Status/stop/start share this
    function, so the fallback covers every judgment path."""
    entry = read_pidfile(name)
    if not entry:
        return None
    pid, lstart = entry
    if not pid_alive(pid):
        return None
    observed = pid_lstart(pid)
    if observed == "":
        # Alive, but no lstart readable (endpoint-security enumeration block,
        # or a `ps` that hit PS_TIMEOUT): identity unverifiable — trust the
        # pidfile.
        return pid
    if observed == lstart:
        return pid
    return None


def proc_exe(pid):
    """realpath of the executable the kernel resolved for `pid`, or None.

    Linux: /proc/<pid>/exe — unreadable for non-dumpable processes (the
    `sg`/`newgrp`-started ones; the same blindness cmd_trim documents), which
    is a legitimate None, not an error. Fallback (macOS, no /proc): `ps -o
    comm=`, trusted ONLY when it is an absolute path — Linux `comm` is the
    15-char truncated name, and realpath() of a bare name would invent a file
    under the cwd. A " (deleted)" suffix (binary replaced since exec) is
    dropped before resolving."""
    try:
        target = os.readlink(f"/proc/{pid}/exe")
        if target.endswith(" (deleted)"):
            target = target[:-len(" (deleted)")]
        return os.path.realpath(target)
    except OSError:
        pass
    try:
        r = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                           stdout=subprocess.PIPE, text=True,
                           timeout=PS_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    comm = r.stdout.strip()
    return os.path.realpath(comm) if comm.startswith("/") else None


def exe_version(exe):
    """`<exe> --version`'s first line, or None. Only EXE_VERSION_ALLOW
    executables are run at all (observing must never execute a binary whose
    semantics are unknown); bounded by VERSION_TIMEOUT, stdin closed so a
    program waiting for input cannot hang, and run under clean_env() so the
    reading depends on the canonical PATH and not on the calling channel."""
    if not exe or not EXE_VERSION_ALLOW.fullmatch(os.path.basename(exe)):
        return None
    try:
        r = subprocess.run([exe, "--version"], cwd=str(WS),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           timeout=VERSION_TIMEOUT, env=clean_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line[:VERSION_LEN]
    return None


def _shebang_exe(path):
    """realpath of the file the kernel execs for `path`: itself when it is not a
    `#!` script, else the shebang interpreter (`env X …` → X) resolved under
    canonical_path() — the same PATH the spawn used. None when unreadable or
    unresolvable (no guessing)."""
    try:
        with open(path, "rb") as f:
            head = f.read(256)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return os.path.realpath(path)
    parts = head.split(b"\n", 1)[0].decode("utf-8", "replace")[2:].split()
    if not parts:
        return None
    interp = parts[0]
    if os.path.basename(interp) == "env" and len(parts) > 1:
        interp = parts[1]
    hit = interp if os.path.isabs(interp) else shutil.which(interp,
                                                           path=canonical_path())
    return os.path.realpath(hit) if hit else None


def expected_exe(svc):
    """The executable this entry's declaration SHOULD resolve to, per the
    canonical PATH — pure disk resolution, no subprocess (status runs it for
    every online row): argv[0] absolute → itself; containing a separator →
    relative to the entry's cwd (cmd is spawned without a shell); a bare name →
    the first hit on canonical_path(), i.e. the very PATH the child got. A `#!`
    script resolves one level further, which is what makes a shebang service
    comparable to the interpreter it really runs. None whenever a step misses:
    nothing to compare against ⇒ display only, never a warning."""
    cmd = svc.get("cmd")
    if not cmd:
        return None
    a0 = _expand(cmd)[0]
    cwd = str(WS / os.path.expanduser(svc.get("cwd") or "."))
    if os.path.isabs(a0):
        cand = a0
    elif os.sep in a0:
        cand = os.path.join(cwd, a0)
    else:
        cand = shutil.which(a0, path=canonical_path())
    if not cand or not os.path.isfile(cand):
        return None
    return _shebang_exe(cand)


def runtime_reading(pid):
    """(exe, interp) of the process just started — each side the literal
    `unknown` when unobtainable (no pid, non-dumpable, binary outside the
    allowlist). Never raises: the reading is supplementary and must not
    become a new way for a start to fail (invariant 10)."""
    try:
        exe = proc_exe(pid) if pid else None
        interp = exe_version(exe) if exe else None
    except Exception as e:            # observation is not load-bearing
        print(f"warning: runtime reading failed ({e!r})", file=sys.stderr)
        exe = interp = None
    return exe or UNKNOWN, interp or UNKNOWN


def write_meta(name, version, pid):
    """Write the meta for a confirmed start: line 1 the bare declared version
    (invariant 11 — every existing reader takes the number from there), then
    the runtime reading of the process this start produced."""
    exe, interp = runtime_reading(pid)
    metafile(name).write_text(
        f"{version}\nexe={exe}\ninterp={interp}\n")


def _kill(pid, sig):
    """Kill one target pid — but if it is a process-group leader, signal the
    whole group. Applies to every stop target (pidfile process and
    pattern-matched instances alike): services we start are detached with
    start_new_session (hence group leaders) and loop scripts keep their real
    daemons as children in the same session, so killing only the leader would
    let the service survive its own stop."""
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
            return
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def svc_entry(name):
    """The service's services.yml entry, or a config error exit."""
    for s in load_services():
        if s["name"] == name:
            return s
    sys.exit(f"error: service {name!r} not declared in {SVC_FILE.name}")


def _lifecycle_name(argv):
    if not argv:
        sys.exit(__doc__)
    return argv[0]


def _expand(args):
    """argv with ~ expansion (shell-style home dirs, e.g. mihomo's
    ~/.config/mihomo): cmd is spawned without a shell, so expand here."""
    return [os.path.expanduser(str(a)) for a in args]


def _validate_entry(svc):
    """Reject malformed lifecycle definitions at the point of use."""
    name = svc.get("name")
    wrapper = svc.get("wrapper", "svc")
    if wrapper not in ("svc", "none"):
        sys.exit(f"error: service {name!r}: wrapper must be 'svc' or 'none', "
                 f"got {wrapper!r}")
    st = svc.get("status")
    if st is not None and not (isinstance(st, dict)
                               and ("http" in st) != ("cmd" in st)):
        sys.exit(f"error: service {name!r}: status must be omitted (proc) "
                 "or exactly one of {http: URL} / {cmd: [...], expect: STR}")
    if svc.get("stop_cmd") and wrapper != "none":
        sys.exit(f"error: service {name!r}: stop_cmd requires wrapper: none")


def probe_online(svc):
    """(online: bool, detail: str) via the entry's status probe.
    proc (default): pidfile alive, else first match-pattern hit.
    http: ANY HTTP response within the timeout = online, error statuses
    included (a service that answers is a service that runs).
    cmd/expect: run the command, online iff output contains `expect`."""
    name = svc["name"]
    st = svc.get("status")
    if st is None:
        pid = pidfile_alive(name)
        if pid is not None:
            return True, f"pid {pid}"
        hits = matched_pids(svc.get("match") or [])
        if hits:
            return True, f"pid {hits[0]}, matched by pattern"
        return False, ""
    if "http" in st:
        url = str(st["http"])
        try:
            with urllib.request.urlopen(url, timeout=st.get("timeout", 5)):
                pass
            return True, url
        except urllib.error.HTTPError:
            return True, url  # any HTTP response counts as online
        except (OSError, ValueError) as e:
            return False, str(e)
    try:
        # env=clean_env(): the probe must run under the same channel-independent
        # PATH as the spawn path (clean_env is its single construction point,
        # shared with build_env and cmd_stop_name) — a `status.cmd` script with
        # an `env python3` shebang would otherwise resolve its interpreter per
        # calling channel and could report a running service as offline.
        out = subprocess.run(_expand(st["cmd"]), cwd=str(WS),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, timeout=PROBE_TIMEOUT,
                             env=clean_env()).stdout.strip()
    except subprocess.TimeoutExpired:
        return False, f"probe cmd timed out after {PROBE_TIMEOUT}s"
    return (str(st.get("expect", "")) in out), out


def _env_kv(line):
    """(key, value) for one decrypted env-file line, or None. envdec.KV_RE is
    the single regex (it takes the `export ` prefix and spaces around `=`);
    quotes are stripped only as a matching pair (same kind on both ends) — an
    unpaired leading or trailing quote is legitimate value content."""
    m = envdec.KV_RE.match(line)
    if not m:
        return None
    head = m.group(1).strip()          # 'export KEY =' | 'KEY ='
    if head.startswith("export "):     # a key literally named exportFOO stays
        head = head[len("export "):].strip()
    key = head.split("=", 1)[0].strip()
    v = m.group(2).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return key, v


def build_env(svc):
    """Spawn environment: scrubbed identity (clean_env), then the data-driven
    env hooks — env_file decryption (encrypt/envdec.py KEY=VALUE lines),
    extra_env (~ expanded), finally the require_env gate: refuse to start
    when a required variable is missing (a service must never come up
    without its credentials)."""
    env = clean_env()
    env_file = svc.get("env_file")
    if env_file:
        # An unreadable or undecryptable file contributes no variables, so the
        # require_env gate below is what refuses the start.
        try:
            text = envdec.decrypt_text((WS / env_file).read_text())
        except Exception:
            text = ""
        for k, v in filter(None, map(_env_kv, text.splitlines())):
            env[k] = v
    for k, v in (svc.get("extra_env") or {}).items():
        env[str(k)] = os.path.expanduser(str(v))
    missing = [k for k in (svc.get("require_env") or []) if not env.get(k)]
    if missing:
        hint = (f"decrypt {env_file} via encrypt/envdec.py or export it"
                if env_file else "export it")
        sys.exit(f"ERROR: {', '.join(missing)} not set ({hint}); refusing "
                 f"to start {svc['name']}")
    return env


def cmd_start_name(argv):
    name = _lifecycle_name(argv)
    svc = svc_entry(name)
    _validate_entry(svc)
    cmd = svc.get("cmd")
    if not cmd:
        sys.exit(f"error: service {name!r} has no 'cmd' in {SVC_FILE.name}")
    wrapper = svc.get("wrapper", "svc")
    # Version drift detection order: READ the declared version first → launch
    # → only after start confirmation WRITE the meta (it records "the
    # declared version that took effect at this start").
    version = str(svc.get("version", "-"))
    PIDS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = _expand(cmd)
    cwd = str(WS / os.path.expanduser(svc.get("cwd") or "."))
    env = build_env(svc)

    if wrapper == "svc":
        pid = pidfile_alive(name)
        if pid is None and svc.get("match"):
            legacy = matched_pids(svc["match"])
            if legacy:
                print(f"{name} already running (pid {legacy[0]}, matched by pattern)")
                return 0
        if pid is not None:
            print(f"{name} already running (pid {pid})")
            return 0
        log = LOGS_DIR / f"{name}.log"
        with open(log, "ab") as lf:
            p = subprocess.Popen(cmd, cwd=cwd, stdout=lf,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True, env=env)
        pidfile(name).write_text(f"{p.pid}\t{pid_lstart(p.pid)}\n")
        # start confirmed: record the declared version this run started under
        # (overwrite; line 1 feeds the `status` version-drift check) plus what
        # the spawned process actually resolved to
        write_meta(name, version, p.pid)
        print(f"{name} started (pid {p.pid}, log {log.relative_to(WS)})")
        return 0

    # wrapper=none: self-daemonizing / external runner (web, maas services).
    # No pidfile, no dedup — the runner owns its restart-in-place (it kills
    # its own old instance). Run in the foreground with stdio inherited, then
    # confirm via the status probe before writing the meta.
    r = subprocess.run(cmd, cwd=cwd, env=env)
    if r.returncode != 0:
        print(f"{name}: start command exited {r.returncode}")
        return r.returncode
    timeout = int(svc.get("start_timeout", 30))
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, _ = probe_online(svc)
        if ok:
            # no pidfile for wrapper=none: the pid comes from the declared match
            # patterns (the same source the proc probe uses), else `unknown`
            hits = matched_pids(svc.get("match") or [])
            write_meta(name, version, hits[0] if hits else None)
            print(f"{name} started (online confirmed, meta v{version})")
            return 0
        time.sleep(1)
    print(f"{name}: start command returned but probe not online within "
          f"{timeout}s — meta not written (status will show 运行 v?)")
    return 0


def cmd_stop_name(argv):
    name = _lifecycle_name(argv)
    svc = svc_entry(name)
    _validate_entry(svc)
    if svc.get("stop_cmd"):
        # external runner owns its own shutdown (maas.py stop)
        r = subprocess.run(_expand(svc["stop_cmd"]), cwd=str(WS),
                           env=clean_env())
        pidfile(name).unlink(missing_ok=True)
        metafile(name).unlink(missing_ok=True)
        print(f"{name} stopped (stop_cmd exit {r.returncode})")
        return r.returncode

    targets = []
    pid = pidfile_alive(name)
    if pid is not None:
        targets.append(pid)
    patterns = list(svc.get("match") or []) + list(svc.get("stop_match") or [])
    for m in matched_pids(patterns):
        if m not in targets:
            targets.append(m)
    if not targets:
        print(f"{name} not running")
        pidfile(name).unlink(missing_ok=True)
        metafile(name).unlink(missing_ok=True)
        return 0

    for t in targets:
        _kill(t, signal.SIGTERM)
    deadline = time.time() + 3
    while time.time() < deadline and any(pid_alive(t) for t in targets):
        time.sleep(0.1)
    rest = [t for t in targets if pid_alive(t)]
    for t in rest:
        _kill(t, signal.SIGKILL)
    time.sleep(0.1)
    gone = ", ".join(str(t) for t in targets)
    stubborn = ", ".join(str(t) for t in rest)
    msg = f"{name} stopped (pid {gone})"
    if stubborn:
        msg += f" [SIGKILL needed: {stubborn}]"
    if any(pid_alive(t) for t in targets):
        msg += " WARNING: some targets still alive"
    print(msg)
    pidfile(name).unlink(missing_ok=True)
    # version meta belongs to the stopped run; removing it keeps "missing
    # meta => ?" honest if the service is later started outside svc.py
    metafile(name).unlink(missing_ok=True)
    return 0


def cmd_status_name(argv):
    name = _lifecycle_name(argv)
    svc = svc_entry(name)
    _validate_entry(svc)
    ok, detail = probe_online(svc)
    st = svc.get("status")
    if isinstance(st, dict) and "cmd" in st and detail:
        print(detail)  # probe output first, then the verdict line
    if ok:
        suffix = f" ({detail})" if (st is None and detail) else ""
        print(f"{name}: online{suffix}")
        exe, interp, expected, warn = _runtime_fields(svc, "online")
        if exe:
            print(f"{name}: runtime exe={exe} interp={interp} "
                  f"声明解析={expected or 'unresolvable'}{warn}")
        return 0
    print(f"{name}: offline")
    return 1


def _dispatch():
    """Exit code for the CLI invocation."""
    argv = sys.argv[1:]
    if argv == ["status", "--machine"]:
        return cmd_status_machine()
    lifecycle = {"start": cmd_start_name, "stop": cmd_stop_name,
                 "status": cmd_status_name}
    if len(argv) >= 2 and argv[0] in lifecycle:
        # `status NAME` is a lifecycle probe; bare `status` is the drift table
        return lifecycle[argv[0]](argv[1:])
    tables = {"status": cmd_status, "sync": cmd_sync, "trim": cmd_trim}
    if len(argv) == 1 and argv[0] in tables:
        return tables[argv[0]]()
    return __doc__  # sys.exit(<str>) semantics: usage to stderr, rc 1


def main():
    try:
        rc = _dispatch()
    except SystemExit as e:
        rc = e.code
    if not isinstance(rc, int):
        if rc:
            print(rc, file=sys.stderr)  # sys.exit(<str>) semantics
        rc = 1 if rc else 0
    sys.exit(rc)  # normal exit: atexit hooks run, streams are flushed


if __name__ == "__main__":
    main()
