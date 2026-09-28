#!/usr/bin/env python3
"""test_git_sync.py — unit tests for svc/git-sync.py (pull-only daemon).

Covers:
+ ① clean worktree        -> ff-only pull, kind `ok`, "ff updated" log line.
+ ② dirty tracked entry   -> kind `skip_dirty`, ONE log line carrying the entry
     list and `behind=N` (the damage scale: N counts origin commits this round
     failed to bring in), and the round still fetches so behind= is accurate.
+ ③ untracked file (ignored and non-ignored) never blocks the pull: git
     overwrites ignored ones silently and leaves the others alone.
+ ④ local unpushed commit -> `skip_ahead`; origin also ahead -> `skip_diverged`
     with a loud WARN and no merge.
+ ⑤ non-ff pull failure   -> `other` + WARN, worktree untouched.
+ ⑥ discover_repos: only top-level dirs that have .git AND are gitignored by
     the main repo enter the sync face (a non-ignored clone does not).
+ ⑦ absent clone          -> `other`, silent.
+ ⑧ end-to-end CLI `--once --root ... --repos .`: exit 0, no mirror fetch in
     test mode.
+ ⑨ red line: the daemon's only output face is the log — no envelope writer,
     no json import, nothing ever written under agents/.
+ ⑩ privacy: dirty file CONTENT never reaches a log line (paths/codes only).

Self-contained: every case runs in a temp sandbox under /tmp (bare origin +
clones). It NEVER writes into ~/m. Run with plain python3 (no pytest needed):
    python3 svc/test_git_sync.py
"""

import atexit
import contextlib
import glob
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gs = _load("git_sync", os.path.join(_HERE, "git-sync.py"))
SRC = open(os.path.join(_HERE, "git-sync.py"), encoding="utf-8").read()

PASS = 0
FAIL = []
SANDBOXES = []
PRIVACY_MARKER = "PRIVACY-MARKER-DO-NOT-EMIT-9f3k"


@atexit.register
def _cleanup_sandboxes():
    """沙箱残留收：断言失败/异常退出时也把 /tmp 沙箱清干净（不留垃圾）。"""
    for tmp in SANDBOXES:
        shutil.rmtree(tmp, ignore_errors=True)


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def git(cwd, *args, check_rc=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd} -> {r.stderr.strip()}")
    return r


def _identity(repo):
    git(repo, "config", "user.name", "git-sync-test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")


def mk_sandbox(files=("f1.txt",), ignore=(), tracked_subrepo=False):
    """Temp workspace: bare origin + a `src` clone (to push from) + `ws` clone
    (the daemon root). Returns (ws, src).

    ignore        = extra .gitignore patterns committed into the main repo
    tracked_subrepo = also create `sub/` as a *tracked* dir (no .gitignore entry)
    """
    tmp = tempfile.mkdtemp(prefix="git-sync-test-")
    SANDBOXES.append(tmp)
    origin = os.path.join(tmp, "origin.git")
    os.makedirs(origin)
    git(origin, "init", "--bare", "-q")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/master")
    src = os.path.join(tmp, "src")
    git(tmp, "clone", "-q", origin, "src")
    _identity(src)
    for fn in files:
        d = os.path.dirname(os.path.join(src, fn))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(src, fn), "w", encoding="utf-8") as fh:
            fh.write(PRIVACY_MARKER + "\nseed content of " + fn + "\n")
    if ignore:
        with open(os.path.join(src, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(ignore) + "\n")
    if tracked_subrepo:
        os.makedirs(os.path.join(src, "sub"), exist_ok=True)
        with open(os.path.join(src, "sub", "keep.txt"), "w", encoding="utf-8") as fh:
            fh.write("tracked dir, not a sub-repo slot\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "seed")
    git(src, "push", "-q", "-u", "origin", "master")
    ws = os.path.join(tmp, "ws")
    git(tmp, "clone", "-q", origin, "ws")
    _identity(ws)
    os.makedirs(os.path.join(ws, "run"), exist_ok=True)
    return ws, src


def push_from(src, rel="f2.txt", msg="upstream commit"):
    """New origin commit (so the ws clone is behind). -f: 也收被 .gitignore 排除
    的路径（③ 要用上游把 agents/ 下路径纳入追踪）。"""
    p = os.path.join(src, rel)
    os.makedirs(os.path.dirname(p) or src, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("new upstream content\n")
    git(src, "add", "-f", "-A")
    git(src, "commit", "-qm", msg)
    git(src, "push", "-q", "origin", "HEAD:master")


def dirty(ws, rel="f1.txt", content=None):
    p = os.path.join(ws, rel)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write((content or "local edit") + "\n")
    return p


def head(ws):
    return git(ws, "rev-parse", "HEAD").stdout.strip()


def round_(ws, name="."):
    """One sync_repo round with stdout captured -> (kind, logtext)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        kind = gs.sync_repo(ws, name)
    return kind, buf.getvalue()


def agents_writes(ws):
    """Anything the daemon created under agents/ (there must be nothing)."""
    d = os.path.join(ws, "agents")
    if not os.path.exists(d):
        return []
    return sorted(glob.glob(os.path.join(d, "**"), recursive=True))


# --------------------------------------------------------------------------
print("\n[①] clean worktree -> ff-only pull")
ws, src = mk_sandbox()
push_from(src)
kind, out = round_(ws)
check("kind = ok", kind == "ok", kind)
check("log shows the ff move", "ff updated" in out, out)
check("HEAD == origin", head(ws) == git(src, "rev-parse", "HEAD").stdout.strip())
kind, out = round_(ws)
check("second round is quiet (already up to date)", kind == "ok" and not out.strip(),
      repr(out))

print("\n[②] dirty tracked entry -> skip_dirty with behind= (damage scale)")
ws, src = mk_sandbox()
dirty(ws)
push_from(src)
push_from(src, rel="f3.txt")
kind, out = round_(ws)
check("kind = skip_dirty", kind == "skip_dirty", kind)
check("one log line", len([l for l in out.splitlines() if l.strip()]) == 1, out)
check("line names the dirty entry", "M f1.txt" in out, out)
check("behind=2 (the round fetched)", "behind=2" in out, out)
check("local edit survived", "local edit" in open(os.path.join(ws, "f1.txt")).read())
check("HEAD not moved", head(ws) != git(src, "rev-parse", "HEAD").stdout.strip())
kind2, out2 = round_(ws)
push_from(src, rel="f4.txt")
kind3, out3 = round_(ws)
check("behind= tracks origin (3 after one more push)", "behind=3" in out3, out3)
check("still skip_dirty", (kind2, kind3) == ("skip_dirty", "skip_dirty"), (kind2, kind3))
# 脏面自愈后即恢复拉取
git(ws, "checkout", "--", "f1.txt")
kind, out = round_(ws)
check("recovery: clean tree pulls again", kind == "ok" and "ff updated" in out, out)

print("\n[③] untracked files never block the pull")
ws, src = mk_sandbox(ignore=("agents/**",))
os.makedirs(os.path.join(ws, "agents", "bot", "x"), exist_ok=True)
with open(os.path.join(ws, "agents", "bot", "x", "spec.json"), "w") as fh:
    fh.write("runtime copy\n")
with open(os.path.join(ws, "loose.txt"), "w") as fh:
    fh.write("untracked, not ignored\n")
push_from(src, rel="agents/bot/x/spec.json")  # 上游把同一路径纳入追踪
kind, out = round_(ws)
check("ignored conflict: git overwrites it, pull succeeds", kind == "ok", (kind, out))
check("tracked version won", "new upstream content" in
      open(os.path.join(ws, "agents", "bot", "x", "spec.json")).read())
check("untracked non-ignored file left alone",
      os.path.exists(os.path.join(ws, "loose.txt")))

print("\n[④] local ahead / diverged")
ws, src = mk_sandbox()
with open(os.path.join(ws, "f1.txt"), "a") as fh:
    fh.write("committed locally\n")
git(ws, "add", "-A")
git(ws, "commit", "-qm", "local commit")
local_head = head(ws)
kind, out = round_(ws)
check("kind = skip_ahead", kind == "skip_ahead", kind)
check("log line says unpushed", "unpushed" in out, out)
push_from(src)
kind, out = round_(ws)
check("origin also ahead -> skip_diverged", kind == "skip_diverged", kind)
check("loud WARN", "WARN DIVERGED" in out, out)
check("no merge happened (HEAD untouched)", head(ws) == local_head)
check("still exactly 1 unpushed commit", git(ws, "rev-list", "--count", "HEAD",
                                            "--not", "--remotes=origin").stdout.strip() == "1")

print("\n[⑤] pull failure -> other + WARN, worktree untouched")
ws, src = mk_sandbox()
before = head(ws)
# 造一个 ff 不可能的局面：把 upstream 指到一个不存在的远端分支
git(ws, "branch", "--set-upstream-to=origin/nope", check_rc=False)
git(ws, "config", "branch.master.merge", "refs/heads/nope")
kind, out = round_(ws)
check("kind = other", kind == "other", kind)
check("WARN logged", "WARN" in out, out)
check("HEAD untouched", head(ws) == before)

print("\n[⑥] discover_repos: ignored top-level .git dirs only")
ws, src = mk_sandbox(ignore=("sub2/",), tracked_subrepo=True)
for d in ("sub1", "sub2", "notarepo"):
    os.makedirs(os.path.join(ws, d), exist_ok=True)
# sub1 = 被主仓追踪的目录（无 .gitignore 条目）里放一个 clone：不入同步面
for d in ("sub1", "sub2"):
    git(ws, "clone", "-q", os.path.join(os.path.dirname(ws), "origin.git"), d + "-tmp")
    shutil.rmtree(os.path.join(ws, d))
    os.rename(os.path.join(ws, d + "-tmp"), os.path.join(ws, d))
repos = gs.discover_repos(ws)
check("main repo always first", repos[0] == ".", str(repos))
check("gitignored clone discovered", "sub2" in repos, str(repos))
check("non-ignored clone NOT discovered", "sub1" not in repos, str(repos))
check("plain dir not discovered", "notarepo" not in repos, str(repos))
check("no stray entries", set(repos) == {".", "sub2"}, str(repos))

print("\n[⑦] absent clone -> other, silent")
ws, src = mk_sandbox()
kind, out = round_(ws, "nosuchrepo")
check("kind = other", kind == "other", kind)
check("no log line", out == "", repr(out))

print("\n[⑧] CLI --once end-to-end (test mode: no mirror fetch)")
ws, src = mk_sandbox()
push_from(src)
r = subprocess.run([sys.executable, os.path.join(_HERE, "git-sync.py"),
                    "--once", "--root", ws, "--repos", "."],
                   capture_output=True, text=True)
check("exit 0", r.returncode == 0, r.stderr[-500:])
check("startup line lists repos", "git-sync started" in r.stdout and "repos=['.']" in r.stdout,
      r.stdout[:400])
check("pulled", "ff updated" in r.stdout, r.stdout[-400:])
check("no mirror fetch in test mode", "mirror" not in r.stdout, r.stdout[-400:])
check("lock file created under run/locks", os.path.exists(os.path.join(ws, "run", "locks",
                                                                     "git-sync.lock")))

print("\n[⑨] red line: the log is the only output face")
check("module exports exactly the sync face", {n for n in dir(gs) if not n.startswith("_")}
      <= {"DEFAULT_ROOT", "DEV_HOST_ID", "DIRTY_LIST_LIMIT", "INFO_FETCH_TIMEOUT",
          "MIRROR_FETCH_EVERY", "MIRROR_FETCH_TIMEOUT", "MIRROR_REPOS", "SCRIPT_DIR",
          "acquire_lock", "ahead_behind", "argparse", "dirty_tracked_entries",
          "discover_repos", "fcntl", "git", "host_id", "log", "main",
          "mirror_fetch", "os", "start_mirror_fetch", "subprocess", "sync_repo",
          "sys", "threading", "time"},
      str(sorted(n for n in dir(gs) if not n.startswith("_"))))
check("no json import (writes no envelopes, reads no pid.json)", "import json" not in SRC)
check("no notify-send", "notify-send" not in SRC)
check("no dispatcher/inbox token", "dispatcher" not in SRC.lower() and "inbox" not in SRC.lower())
check("no agents/ coupling", "agents/" not in SRC)
ws, src = mk_sandbox()
dirty(ws)
push_from(src)
for _ in range(3):
    round_(ws)
check("SKIP rounds write nothing under agents/", agents_writes(ws) == [],
      str(agents_writes(ws)))

print("\n[⑩] privacy: dirty content never logged")
ws, src = mk_sandbox()
dirty(ws, content=PRIVACY_MARKER)
kind, out = round_(ws)
check("marker absent from the log line", PRIVACY_MARKER not in out, out)
check("entry path present instead", "f1.txt" in out, out)

print()
print(f"{PASS} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
print("ALL OK")
