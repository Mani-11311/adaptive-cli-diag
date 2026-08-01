"""Quick test harness — runs every command and checks for failures."""
import json, subprocess, sys

PASS = "[PASS]"
FAIL = "[FAIL]"
results = []

def run(label, args, check_str=None):
    r = subprocess.run(
        [sys.executable, "main.py"] + args,
        capture_output=True, text=True
    )
    ok = r.returncode == 0
    if check_str:
        ok = ok and check_str in r.stdout
    tag = PASS if ok else FAIL
    print(f"{tag}  {label}")
    if not ok:
        print("      STDOUT:", r.stdout[:300])
        print("      STDERR:", r.stderr[:300])
    results.append(ok)

print("\n=== adaptive-cli-diag — full test run ===\n")

run("--version",                   ["--version"],              "v1.2.0")
run("--help",                      ["--help"],                 "diagnose")
run("diagnose (snapshot)",         ["diagnose"],               "Adaptive CLI Diagnostics")
run("diagnose --help",             ["diagnose", "--help"],     "--watch")
run("top (cpu sort)",              ["top", "-n", "3"],         "sorted by CPU")
run("top --sort mem",              ["top", "-n", "3", "--sort", "mem"], "sorted by MEM")
run("top --help",                  ["top", "--help"],          "--sort")
run("config (view)",               ["config"],                 "Current Configuration")
run("config --cpu-warn 65",        ["config", "--cpu-warn", "65"], "Updated 1")
run("config (changed shown)",      ["config"],                 "CPU Warning")
run("config --reset",              ["config", "--reset"],      "reset to defaults")
run("config (back to default)",    ["config"],                 "Current Configuration")
run("report report.json",          ["report", "report.json"],  "Report saved")
run("ping-check (single host)",    ["ping-check", "8.8.8.8"], "Reachable")
run("ping-check (multi host)",     ["ping-check", "google.com", "github.com", "1.1.1.1"], "reachable")
run("ping-check (bad host crit)",  ["ping-check", "definitelynotavalidhostname123.xyz"], "Ping Check")
run("ping-check --count 2",        ["ping-check", "8.8.8.8", "--count", "2"], "Reachable")

# Validate report.json structure
print()
try:
    with open("report.json") as f:
        d = json.load(f)
    checks = [
        ("timestamp",     "timestamp" in d),
        ("cpu.overall",   isinstance(d["cpu"]["overall"], float) and d["cpu"]["overall"] > 0),
        ("cpu.per_core",  len(d["cpu"]["per_core"]) > 0),
        ("memory.%",      isinstance(d["memory"]["percent"], float)),
        ("disks",         len(d["disks"]) > 0),
        ("network",       "bytes_sent" in d["network"]),
        ("top_processes", len(d["top_processes"]) > 0),
        ("top procs CPU real", any(p["cpu_percent"] > 0 for p in d["top_processes"])),
        ("config_used",   "cpu_warn" in d["config_used"]),
    ]
    for label, ok in checks:
        tag = PASS if ok else FAIL
        print(f"{tag}  report.json → {label}")
        results.append(ok)
    print()
    print(f"  CPU overall:  {d['cpu']['overall']}%")
    print(f"  Memory:       {d['memory']['percent']}%")
    print(f"  Disks:        {len(d['disks'])}")
    print(f"  Top procs:    {len(d['top_processes'])}")
    for p in d["top_processes"]:
        print(f"    {p['name']:28s}  cpu={p['cpu_percent']:.1f}%  mem={p['memory_percent']:.1f}%")
    print(f"  Timestamp:    {d['timestamp']}")
except Exception as e:
    print(f"{FAIL}  report.json parse error: {e}")
    results.append(False)

print()
passed = sum(results)
total  = len(results)
print(f"{'='*42}")
print(f"  Result: {passed}/{total} tests passed")
if passed == total:
    print("  All tests PASSED.")
else:
    print(f"  {total - passed} test(s) FAILED.")
print(f"{'='*42}\n")
sys.exit(0 if passed == total else 1)
