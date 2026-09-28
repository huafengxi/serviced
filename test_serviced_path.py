#!/usr/bin/env python3
"""test_serviced_path.py — unit tests for serviced/serviced.py's
canonical_path() (the channel-independent PATH service children get).

Everything runs on SYNTHETIC trees under tempfile.mkdtemp(): no service is
started or stopped, no pidfile/meta is touched, nothing is written into the
workspace. Sandboxes live in the SYSTEM temp dir (never under a production
root) and are removed at exit only after a realpath identity assertion
(_sandbox_ok) — a path that is a production root, an ancestor of one, or inside
one is refused and left alone.

Covers (the six semantic faces of canonical_path):
+ ① existing directories only — a declared segment that is not a directory
     contributes nothing.
+ ② dedupe, first occurrence wins — a repeat (inside the prefix, or again in
     the inherited tail) keeps its first position.
+ ③ empty entries dropped — `::` in either source never yields a "" segment
     (an empty PATH entry means the cwd: an accident, never an intent).
+ ④ the SERVICED_PATH_PREFIX override's three states — unset = the module's
     built-in prefix; non-empty = wholesale replacement (no built-in segment
     survives); empty string = no prefix at all = the pre-normalization
     behaviour.
+ ⑤ glob hits ALL enter, in natural-version DESCENDING order (not "only the
     highest branch"): the fixture includes the malformed names `system` and
     `8` to cover _version_key's mixed int/str comparison.
+ ⑥ an inherited tail that contributes nothing (empty OR unset) ⇒ the output
     carries os.defpath's system floor (/bin, /usr/bin) — the fix that keeps
     `cmd: [bash, …]` entries startable. Locked here so it cannot later be
     deleted as "redundant".
+ ⑦ with a non-empty inherited tail the output is VERBATIM what the prefix +
     tail rules produce — the floor adds nothing (expected value constructed
     by hand below, never by calling the function under test).
+ ⑧ the sandbox identity guard itself: it refuses production roots (decoy
     inputs only — no rmtree is ever aimed at them).

Run: python3 test_serviced_path.py     (exit 1 = at least one FAIL)
"""

import atexit
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SEP = os.pathsep


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


serviced = _load("serviced_under_test", os.path.join(HERE, "serviced.py"))

PASS = 0
FAIL = []
SANDBOXES = []

# Production roots the sandbox guard refuses (decoy inputs for case ⑧; the
# guard is a pure predicate — nothing here is ever passed to rmtree).
HOME = os.path.realpath(os.path.expanduser("~"))
PROD_ROOTS = [HOME]
for rel in ("m", "m/lore", "m/work", "m/pi-core"):
    p = os.path.join(HOME, rel)
    if os.path.isdir(p):
        PROD_ROOTS.append(os.path.realpath(p))
TMPROOT = os.path.realpath(tempfile.gettempdir())


def _sandbox_ok(path):
    """Identity predicate for cleanup: `path` must resolve to something STRICTLY
    inside the system temp dir AND unrelated to any production root (not equal,
    not an ancestor, not inside). Deliberately no exemption for temp dirs that
    happen to sit under a production root: an exemption's width would itself be
    a risk face."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    if real == TMPROOT or not real.startswith(TMPROOT + os.sep):
        return False
    for prod in PROD_ROOTS:
        if real == prod or prod.startswith(real + os.sep) \
                or real.startswith(prod + os.sep):
            return False
    return True


@atexit.register
def _cleanup():
    """Remove the sandboxes — also on assertion failure — but only after the
    identity assertion; a refused path is left untouched and reported."""
    for root in SANDBOXES:
        if _sandbox_ok(root):
            shutil.rmtree(root)          # no ignore_errors: failures must show
        else:
            print(f"  REFUSED to delete {root} (identity assertion failed)")


def ok(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def mk_sandbox():
    root = tempfile.mkdtemp(prefix="serviced-path-test-")
    SANDBOXES.append(root)
    return root


def mkdirs(root, *names):
    """Create <root>/<name> directories, return their absolute paths."""
    out = []
    for n in names:
        d = os.path.join(root, n)
        os.makedirs(d, exist_ok=True)
        out.append(d)
    return out


@contextlib.contextmanager
def prefix(specs, override="unset", inherited=None, drop_path=False):
    """Run canonical_path() against a synthetic built-in prefix and a controlled
    environment. override: "unset" = remove SERVICED_PATH_PREFIX; any other string is
    set verbatim ("" included = the empty-string state). inherited: value for
    PATH (None = leave the environment's own PATH alone)."""
    saved = (serviced.PATH_PREFIX,
             os.environ.get("SERVICED_PATH_PREFIX", "unset"),
             os.environ.get("PATH", "unset"))
    serviced.PATH_PREFIX = tuple(specs)
    if override == "unset":
        os.environ.pop("SERVICED_PATH_PREFIX", None)
    else:
        os.environ["SERVICED_PATH_PREFIX"] = override
    if drop_path:
        os.environ.pop("PATH", None)
    elif inherited is not None:
        os.environ["PATH"] = inherited
    try:
        yield
    finally:
        serviced.PATH_PREFIX = saved[0]
        for key, val in (("SERVICED_PATH_PREFIX", saved[1]), ("PATH", saved[2])):
            if val == "unset":
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def parts(out):
    return out.split(SEP)


print("\n[⑧] sandbox identity guard (decoys only, no deletion attempted)")
sb = mk_sandbox()
ok("real sandbox accepted", _sandbox_ok(sb), sb)
ok("a production root refused", not _sandbox_ok(PROD_ROOTS[-1]), PROD_ROOTS[-1])
ok("$HOME refused", not _sandbox_ok(HOME), HOME)
ok("/ refused", not _sandbox_ok("/"))
ok("the temp root itself refused", not _sandbox_ok(TMPROOT), TMPROOT)
ok("inside a production root refused",
    not _sandbox_ok(os.path.join(PROD_ROOTS[-1], "run", "temp", "x")))
ok("an ancestor of a production root refused",
    not _sandbox_ok(os.path.dirname(PROD_ROOTS[-1])))

print("\n[①] existing directories only")
root = mk_sandbox()
a, b = mkdirs(root, "a", "b")
missing = os.path.join(root, "nope")
inh = mkdirs(root, "inh")[0]
with prefix((), override=SEP.join([a, missing, b]), inherited=inh):
    out = serviced.canonical_path(inh)
ok("existing prefix segments enter", a in parts(out) and b in parts(out), out)
ok("a missing segment contributes nothing", missing not in parts(out), out)
ok("inherited tail kept", parts(out) == [a, b, inh], out)

print("\n[②] dedupe, first occurrence wins")
root = mk_sandbox()
a, d1, d2, new = mkdirs(root, "a", "d1", "d2", "new")
with prefix((), override=SEP.join([a, d1, d2]), inherited=SEP.join([d2, a, new])):
    out = serviced.canonical_path(SEP.join([d2, a, new]))
ok("each directory appears once", len(parts(out)) == len(set(parts(out))), out)
ok("first position wins (prefix order kept, repeats dropped)",
    parts(out) == [a, d1, d2, new], out)
with prefix((a, a), override="unset", inherited=a):
    out = serviced.canonical_path(a)
ok("a repeated built-in segment appears once", parts(out) == [a], out)

print("\n[③] empty entries dropped")
root = mk_sandbox()
a = mkdirs(root, "a")[0]
inh = mkdirs(root, "inh")[0]
with prefix((), override=SEP.join(["", a, ""]), inherited=SEP.join(["", inh, ""])):
    out = serviced.canonical_path(SEP.join(["", inh, ""]))
ok("no empty segment in the output", "" not in parts(out), out)
ok("the real segments survive", parts(out) == [a, inh], out)

print("\n[④] SERVICED_PATH_PREFIX override: three states")
root = mk_sandbox()
p1, p2, o1, inh = mkdirs(root, "p1", "p2", "o1", "inh")
# unset -> the module's built-in prefix (swapped to a synthetic pair)
with prefix((p1, p2), override="unset", inherited=inh):
    out_unset = serviced.canonical_path(inh)
ok("unset = built-in prefix + inherited tail", parts(out_unset) == [p1, p2, inh],
    out_unset)
# non-empty -> wholesale replacement: no built-in segment survives
with prefix((p1, p2), override=o1, inherited=inh):
    out_set = serviced.canonical_path(inh)
ok("non-empty override replaces the prefix wholesale",
    parts(out_set) == [o1, inh], out_set)
ok("no built-in segment survives the replacement",
    p1 not in parts(out_set) and p2 not in parts(out_set), out_set)
# empty string -> no prefix at all (the pre-normalization behaviour)
with prefix((p1, p2), override="", inherited=inh):
    out_empty = serviced.canonical_path(inh)
ok("empty-string override = no prefix (pre-normalization behaviour)",
    parts(out_empty) == [inh], out_empty)
ok("empty-string override drops every prefix segment",
    not ({p1, p2} & set(parts(out_empty))), out_empty)

print("\n[⑤] glob hits all enter, natural-version descending")
root = mk_sandbox()
nv = os.path.join(root, "nv")
for v in ("v8", "v10", "v22", "system", "8"):
    os.makedirs(os.path.join(nv, v, "bin"))
inh = mkdirs(root, "inh")[0]
pattern = os.path.join(nv, "*", "bin")
with prefix((), override=pattern, inherited=inh):
    out = serviced.canonical_path(inh)
got = parts(out)
hits = [p for p in got if p.startswith(nv + os.sep)]
# Expected order derived by hand from _version_key (digit runs compare as
# numbers, so v22 > v10 > v8 — lexicographic would give v8 > v22) and from the
# plain-string comparison that separates the two malformed names: their first
# split element is "<nv>/8/bin" vs "<nv>/system/bin" vs "<nv>/v" ⇒ after the
# common "<nv>/" prefix the deciding characters are 'v'(118) > 's'(115) >
# '8'(56), and the version numbers then order the v* branch. No int/str
# comparison is reached: element 0 already differs.
expected_hits = [os.path.join(nv, v, "bin") for v in ("v22", "v10", "v8",
                                                      "system", "8")]
ok("every glob hit enters (not only the highest branch)", len(hits) == 5, str(hits))
ok("natural-version descending order", hits == expected_hits,
    f"got {hits}\n       want {expected_hits}")
ok("the inherited tail still follows the glob hits", got[-1] == inh, out)

print("\n[⑥] inherited tail contributes nothing => os.defpath floor")
root = mk_sandbox()
p1 = mkdirs(root, "p1")[0]
floor = [d for d in os.defpath.split(SEP) if d]
with prefix((p1,), override="unset", inherited=""):
    out_empty_inh = serviced.canonical_path("")
with prefix((p1,), override="unset", drop_path=True):
    out_no_path = serviced.canonical_path()
for label, out in (("empty inherited PATH", out_empty_inh),
                   ("unset PATH", out_no_path)):
    got = parts(out)
    ok(f"{label}: prefix kept, floor appended", got[0] == p1, out)
    ok(f"{label}: every os.defpath entry present",
        all(d in got for d in floor), f"{out} vs defpath {os.defpath}")
    ok(f"{label}: the floor is the tail (nothing inherited between)",
        got[-len(floor):] == floor, out)
    for literal in ("/bin", "/usr/bin"):
        if literal in floor:
            ok(f"{label}: output contains {literal}", literal in got, out)

print("\n[⑦] with an inherited tail the output is verbatim (floor adds nothing)")
root = mk_sandbox()
p1, p2 = mkdirs(root, "p1", "p2")
gone = os.path.join(root, "gone")
i1, i2 = mkdirs(root, "i1", "i2")
declared = (p1, gone, p2, p1)              # a missing segment + a repeat
inherited = SEP.join([p2, "", i1, i2, i1])  # a repeat, an empty entry
# Expected value constructed BY HAND from the documented rules (existing only,
# first occurrence wins, empty entries dropped, prefix then tail) — NOT by
# calling canonical_path/_prefix_dirs, which would make the assertion circular.
manual = []
for d in declared:
    if os.path.isdir(d) and d not in manual:
        manual.append(d)
for d in inherited.split(SEP):
    if d and d not in manual:
        manual.append(d)
manual_out = SEP.join(manual)
with prefix(declared, override="unset", inherited=inherited):
    out = serviced.canonical_path(inherited)
ok("hand-built expectation equals the function's output, byte for byte",
    out == manual_out, f"got  {out}\n       want {manual_out}")
ok("no os.defpath entry was appended (the tail contributed)",
    not (set(parts(out)) & set(floor) - set(manual)), out)
ok("and it equals the pre-floor behaviour (tail non-empty ⇒ floor inert)",
    parts(out) == [p1, p2, i1, i2], out)

print()
print(f"{PASS} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
print("ALL OK")
