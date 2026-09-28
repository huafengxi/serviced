#!/usr/bin/env python3
"""test_registry.py — unit tests for serviced.py's two-layer registry loader
(services/profiles/*.json + services/daemons/<host>.<service>.json).

Everything runs on SYNTHETIC trees under tempfile.mkdtemp(): no service is
started or stopped, no pidfile/meta is touched, nothing is written into a real
workspace, and the module under test is loaded by path (no import side effects
beyond its own constants).

Covers:
+ ① profiles: the name IS the file stem; a disagreeing `name` key, an unknown
     key, a non-integer `version`, bad JSON, a non-object top level and an
     empty profiles dir are each a config error (exit), never a silent skip.
+ ② daemons: filename shape `<host>.<service>.json` (host dot-free); the
     `profile` key must agree with the filename's service segment; `desired`
     must be online|offline; missing/unknown keys are errors.
+ ③ host matching: a declaration applies when its `hostname` equals this
     machine's hostname or is a dot-separated prefix of it, case-insensitively;
     anything else stays out of this machine's face.
+ ④ host-id map cross-check: when the map resolves a declaration's hostname,
     the filename's host segment must equal that canonical name (a declaration
     copied to a new machine without editing `hostname` is caught here instead
     of silently matching nobody); an unresolvable hostname skips the check.
+ ⑤ services_here: the face is exactly this machine's declarations, sorted by
     service name, each row = the profile's fields verbatim + `desired` from the
     daemon; a dangling profile and two declarations for one service on one
     machine are errors.
+ ⑥ an empty face is not an error but warns loudly on stderr (an unmanaged
     machine must not read as a healthy `0 drift`).

Run: python3 test_registry.py          (exit 1 = at least one FAIL)
"""

import atexit
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sv = _load("serviced_under_test", os.path.join(HERE, "serviced.py"))

PASS = 0
FAIL = []
SANDBOXES = []


@atexit.register
def _cleanup():
    for d in SANDBOXES:
        shutil.rmtree(d, ignore_errors=True)


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def tree(profiles=None, daemons=None, host_id=None):
    """(root, profiles_dir, daemons_dir, host_id_file) for a synthetic registry.
    `profiles`/`daemons` map filename stem → dict (or a raw string, written
    verbatim, for the bad-JSON cases)."""
    root = tempfile.mkdtemp(prefix="serviced-registry-test-")
    SANDBOXES.append(root)
    pdir = os.path.join(root, "services", "profiles")
    ddir = os.path.join(root, "services", "daemons")
    os.makedirs(pdir)
    os.makedirs(ddir)
    for stem, body in (profiles or {}).items():
        _write(os.path.join(pdir, stem + ".json"), body)
    for stem, body in (daemons or {}).items():
        _write(os.path.join(ddir, stem + ".json"), body)
    hif = os.path.join(root, "host-id")
    if host_id is not None:
        with open(hif, "w", encoding="utf-8") as f:
            f.write(host_id)
    return root, pdir, ddir, (hif if host_id is not None else None)


def _write(path, body):
    with open(path, "w", encoding="utf-8") as f:
        f.write(body if isinstance(body, str)
                else json.dumps(body, ensure_ascii=False, indent=2))


def err(fn, *a, **kw):
    """Run fn, returning the SystemExit message ('' when it did not exit)."""
    try:
        fn(*a, **kw)
    except SystemExit as e:
        return str(e.code)
    return ""


HOST = "node1.example.com"          # this machine's hostname in the fixtures
PROFILES = {
    "alpha": {"summary": "α", "version": 3, "cmd": ["python3", "a.py"],
              "match": ["^python3 a\\.py$"]},
    "beta": {"summary": "β", "version": 7, "wrapper": "none",
             "cmd": ["./run.sh"], "stop_cmd": ["./run.sh", "stop"],
             "status": {"cmd": ["./run.sh", "status"], "expect": "UP"},
             "env_file": "env/beta.env", "require_env": ["BETA_TOKEN"],
             "extra_env": {"BETA_HOME": "~/beta"}},
}


def daemon(hostname, profile, desired="online", **extra):
    d = {"hostname": hostname, "profile": profile, "desired": desired}
    d.update(extra)
    return d


print("\n[①] profiles: stem is the name; malformed shapes are config errors")
root, pdir, ddir, hif = tree(profiles=PROFILES)
got = sv.load_profiles(pdir)
check("both profiles loaded", sorted(got) == ["alpha", "beta"], str(sorted(got)))
check("name injected from the stem", got["alpha"]["name"] == "alpha")
check("lifecycle fields verbatim",
      got["beta"]["status"] == {"cmd": ["./run.sh", "status"], "expect": "UP"}
      and got["beta"]["require_env"] == ["BETA_TOKEN"]
      and got["beta"]["extra_env"] == {"BETA_HOME": "~/beta"}
      and got["beta"]["wrapper"] == "none", str(got["beta"]))

root, pdir, _, _ = tree(profiles={"alpha": {"version": 1, "name": "other"}})
check("a `name` key that disagrees with the stem is an error",
      "single source" in err(sv.load_profiles, pdir))

root, pdir, _, _ = tree(profiles={"alpha": {"version": 1, "cmd": ["x"], "cmnd": ["y"]}})
check("an unknown key is an error (a typo must not be silently ignored)",
      "unknown key" in err(sv.load_profiles, pdir), err(sv.load_profiles, pdir))

root, pdir, _, _ = tree(profiles={"alpha": {"version": "3"}})
check("a non-integer version is an error", "integer" in err(sv.load_profiles, pdir))

root, pdir, _, _ = tree(profiles={"alpha": '{"version": 1,}'})
check("bad JSON is an error naming the file",
      "invalid JSON" in err(sv.load_profiles, pdir))

root, pdir, _, _ = tree(profiles={"alpha": [1, 2]})
check("a non-object top level is an error", "JSON object" in err(sv.load_profiles, pdir))

root, pdir, _, _ = tree(profiles={})
check("an empty profiles dir is an error", "no service profiles" in err(sv.load_profiles, pdir))

print("\n[②] daemons: filename shape, profile agreement, desired enum")
root, pdir, ddir, _ = tree(daemons={"node1.alpha": daemon("node1", "alpha")})
rows = sv.load_daemons(ddir)
check("one declaration parsed", len(rows) == 1 and rows[0][1] == "node1", str(rows))
check("declaration body kept", rows[0][2]["profile"] == "alpha", str(rows))

for stem, body, token in [
    ("alpha", daemon("node1", "alpha"), "<host>.<service>.json"),
    ("node1.sub.alpha", daemon("node1", "alpha"), "<host>.<service>.json"),
    ("node1.alpha", daemon("node1", "beta"), "must agree"),
    ("node1.alpha", daemon("node1", "alpha", desired="up"), "'desired' must be one of"),
    ("node1.alpha", {"hostname": "node1", "desired": "online"}, "missing key"),
    ("node1.alpha", daemon("node1", "alpha", host="node1"), "unknown key"),
]:
    root, _, ddir, _ = tree(daemons={stem: body})
    msg = err(sv.load_daemons, ddir)
    check(f"{stem} / {sorted(body)} → error mentioning {token!r}", token in msg, msg)

print("\n[③] host matching: equal, dot-prefix, case-insensitive, else out")
check("exact hostname matches", sv._host_matches("node1.example.com", "node1.example.com"))
check("dot-separated prefix matches", sv._host_matches("node1.example.com", "node1"))
check("match is case-insensitive", sv._host_matches("Node1.Example.COM", "node1"))
check("a partial label does NOT match", not sv._host_matches("node11.example.com", "node1"))
check("a different host does NOT match", not sv._host_matches("node2.example.com", "node1"))

root, pdir, ddir, _ = tree(
    profiles=PROFILES,
    daemons={"node1.alpha": daemon("node1", "alpha"),
             "node9.beta": daemon("node9", "beta")})
face = sv.services_here(hostname=HOST, profiles=sv.load_profiles(pdir),
                        daemons=sv.load_daemons(ddir))
check("only this machine's declarations enter the face",
      [s["name"] for s in face] == ["alpha"], str([s["name"] for s in face]))

print("\n[④] host-id map cross-check (filename host segment vs canonical name)")
root, pdir, ddir, hif = tree(
    profiles=PROFILES,
    daemons={"node1.alpha": daemon(HOST, "alpha")},
    host_id=f"# map\n{HOST}\tnode1\n")
check("canonical name agrees with the filename ⇒ no error",
      err(sv.load_daemons, ddir, hif) == "", err(sv.load_daemons, ddir, hif))

root, pdir, ddir, hif = tree(
    profiles=PROFILES,
    daemons={"node1.alpha": daemon("other-host", "alpha")},
    host_id=f"{HOST}\tnode1\nother-host\tnode7\n")
msg = err(sv.load_daemons, ddir, hif)
check("a filename that disagrees with the map is an error",
      "canonical" in msg and "node7" in msg, msg)

root, pdir, ddir, hif = tree(
    profiles=PROFILES,
    daemons={"node1.alpha": daemon("unmapped-host", "alpha")},
    host_id=f"{HOST}\tnode1\n")
check("an unmapped hostname skips the cross-check (map is not the authority)",
      err(sv.load_daemons, ddir, hif) == "", err(sv.load_daemons, ddir, hif))

check("host_id_map parses `<hostname> <canonical>` and skips comments/blanks",
      sv.host_id_map(hif) == {HOST.lower(): "node1"}, str(sv.host_id_map(hif)))
check("a missing map file yields {}", sv.host_id_map("/nonexistent/host-id") == {})

print("\n[⑤] services_here: face shape, merge, order, errors")
root, pdir, ddir, _ = tree(
    profiles=PROFILES,
    daemons={"node1.beta": daemon("node1", "beta", "offline", note="拍板 offline"),
             "node1.alpha": daemon("node1", "alpha")})
face = sv.services_here(hostname=HOST, profiles=sv.load_profiles(pdir),
                        daemons=sv.load_daemons(ddir))
check("sorted by service name", [s["name"] for s in face] == ["alpha", "beta"],
      str([s["name"] for s in face]))
check("desired comes from the daemon declaration",
      face[0]["desired"] == "online" and face[1]["desired"] == "offline")
check("lifecycle fields come from the profile verbatim",
      face[1]["stop_cmd"] == ["./run.sh", "stop"] and face[1]["version"] == 7,
      str(face[1]))
check("the profile is not mutated by the merge",
      sv.load_profiles(pdir)["beta"].get("desired") is None)

root, pdir, ddir, _ = tree(profiles=PROFILES,
                           daemons={"node1.gamma": daemon("node1", "gamma")})
check("a dangling profile is an error naming the missing file",
      "does not exist" in err(sv.services_here, HOST, None, None, pdir, ddir),
      err(sv.services_here, HOST, None, None, pdir, ddir))

root, pdir, ddir, _ = tree(
    profiles=PROFILES,
    # two files, different host segments, both hostnames matching this machine
    daemons={"node1.alpha": daemon(HOST, "alpha"),
             "n1.alpha": daemon("node1", "alpha")})
msg = err(sv.services_here, HOST, None, None, pdir, ddir)
check("two declarations for one service on one machine is an error",
      "both declare" in msg, msg)

root, pdir, ddir, _ = tree(profiles=PROFILES,
                           daemons={"node1.al.pha": daemon("node1", "al.pha")})
msg = err(sv.load_daemons, ddir)
check("a dotted service name cannot be expressed ⇒ filename shape error",
      "<host>.<service>.json" in msg, msg)

print("\n[⑥] an empty face warns loudly and is not an error")
root, pdir, ddir, _ = tree(profiles=PROFILES,
                           daemons={"node9.alpha": daemon("node9", "alpha")})
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    face = sv.services_here(hostname=HOST, profiles=sv.load_profiles(pdir),
                            daemons=sv.load_daemons(ddir))
check("empty face returns []", face == [], str(face))
check("and warns on stderr (an unmanaged machine must not read as healthy)",
      "no daemon declaration matches this machine" in buf.getvalue(), buf.getvalue())

print()
print(f"{PASS} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
print("ALL OK")
