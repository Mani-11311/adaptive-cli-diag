from __future__ import annotations
import json
import math
import platform
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import psutil
import typer
from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# App identity
APP_NAME    = "adaptive-cli-diag"
APP_VERSION = "1.2.0"
CONFIG_DIR  = Path.home() / ".config" / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"

# Platform info gathered once at startup
_HOSTNAME = platform.node()
_OS_INFO  = f"{platform.system()} {platform.release()}"
_CPU_RAW  = platform.processor() or "Unknown CPU"
# Trim to a clean word boundary so the header doesn't break mid-token
_CPU_NAME = _CPU_RAW if len(_CPU_RAW) <= 36 else _CPU_RAW[:36].rsplit(" ", 1)[0] + "..."
_IS_WIN   = platform.system().lower() == "windows"

# Severity levels in ascending order — index is used for ranking
_SEV_RANK: tuple[str, ...] = ("ok", "warn", "crit")
_SEV_ICON: dict[str, str]  = {"ok": "[OK]", "warn": "[!!]", "crit": "[XX]"}

_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

CUSTOM_THEME = Theme({
    "ok":     "bold green",
    "warn":   "bold yellow",
    "crit":   "bold red",
    "info":   "bold cyan",
    "muted":  "dim white",
    "metric": "bold #00d4ff",
    "label":  "#a0a0c0",
})

console = Console(theme=CUSTOM_THEME, file=open(1, mode="w", encoding="utf-8", closefd=False) if _IS_WIN else None)
app = typer.Typer(
    name="diag",
    help="Adaptive CLI Diagnostics — real-time system health monitor.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

DEFAULT_CONFIG: dict[str, int] = {
    "cpu_warn":  70,  "cpu_crit":  90,
    "mem_warn":  75,  "mem_crit":  90,
    "disk_warn": 80,  "disk_crit": 95,
    "temp_warn": 75,  "temp_crit": 90,
    "interval":   2,  "top_procs":  5,
}


# Config I/O

def load_config() -> dict:
    """Load saved config, falling back to defaults for any missing key."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                return {**DEFAULT_CONFIG, **json.load(fh)}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


# Helpers

def severity(value: float, warn: float, crit: float) -> str:
    """Return the Rich style name that matches the value's severity level."""
    return "crit" if value >= crit else "warn" if value >= warn else "ok"


def sev_icon(style: str) -> str:
    return _SEV_ICON.get(style, "?")


def worst_severity(styles: list[str]) -> str:
    """Return the most severe style from a list."""
    return max(styles, key=_SEV_RANK.index) if styles else "ok"


def gauge_bar(value: float, warn: float, crit: float, width: int = 20) -> Text:
    """ASCII progress bar that changes colour at warn/crit thresholds."""
    filled = int(min(value, 100.0) / 100.0 * width)
    bar = "#" * filled + "-" * (width - filled)
    return Text(f"[{bar}]", style=severity(value, warn, crit))


def fmt_bytes(n: float) -> str:
    """Convert a byte count to a human-readable string (e.g. 1.4 GB)."""
    if n <= 0:
        return "0 B"
    idx = min(int(math.log(n, 1024)), len(_BYTE_UNITS) - 1)
    return f"{n / 1024 ** idx:.1f} {_BYTE_UNITS[idx]}"


def fmt_freq(mhz: float | None) -> str:
    if mhz is None:
        return "N/A"
    return f"{mhz / 1000:.2f} GHz" if mhz >= 1000 else f"{mhz:.0f} MHz"


# Metric collectors

def get_cpu_metrics(cfg: dict) -> dict:
    """
    Sample CPU usage with a short blocking interval for an accurate reading.
    Overall usage is derived from the per-core values to avoid a redundant call
    (a second immediate call always returns 0.0 since there is no elapsed time).
    """
    per_core: list[float] = psutil.cpu_percent(interval=0.4, percpu=True)  # type: ignore[assignment]
    overall  = round(sum(per_core) / len(per_core), 1) if per_core else 0.0
    freq     = psutil.cpu_freq()
    ctx      = psutil.cpu_stats()
    return {
        "overall":      overall,
        "per_core":     per_core,
        "freq_current": freq.current if freq else None,
        "freq_max":     freq.max     if freq else None,
        "ctx_switches": ctx.ctx_switches,
        "interrupts":   ctx.interrupts,
        "style":        severity(overall, cfg["cpu_warn"], cfg["cpu_crit"]),
    }


def get_mem_metrics(cfg: dict) -> dict:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "total":        vm.total,
        "used":         vm.used,
        "available":    vm.available,
        "percent":      vm.percent,
        "swap_total":   sw.total,
        "swap_used":    sw.used,
        "swap_percent": sw.percent,
        "style":        severity(vm.percent, cfg["mem_warn"], cfg["mem_crit"]),
    }


def get_disk_metrics(cfg: dict) -> list[dict]:
    results = []
    dw, dc = cfg["disk_warn"], cfg["disk_crit"]
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        results.append({
            "device":     part.device,
            "mountpoint": part.mountpoint,
            "fstype":     part.fstype,
            "total":      u.total,
            "used":       u.used,
            "free":       u.free,
            "percent":    u.percent,
            "style":      severity(u.percent, dw, dc),
        })
    return results


def get_net_metrics() -> dict:
    io = psutil.net_io_counters()
    return {
        "bytes_sent":   io.bytes_sent,
        "bytes_recv":   io.bytes_recv,
        "packets_sent": io.packets_sent,
        "packets_recv": io.packets_recv,
        "errin":  io.errin,  "errout":  io.errout,
        "dropin": io.dropin, "dropout": io.dropout,
    }


def get_temp_metrics(cfg: dict) -> list[dict]:
    results: list[dict] = []
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return results
        tw, tc = cfg["temp_warn"], cfg["temp_crit"]
        for sensor_name, entries in temps.items():
            for entry in entries:
                results.append({
                    "sensor":   sensor_name,
                    "label":    entry.label or sensor_name,
                    "current":  entry.current,
                    "high":     entry.high,
                    "critical": entry.critical,
                    "style":    severity(entry.current, tw, tc),
                })
    except AttributeError:
        pass  # Not supported on this platform (e.g. Windows without WMI sensors)
    return results


def get_top_processes(n: int = 5) -> list[dict]:
    """
    Return the top-n processes sorted by CPU usage.
    psutil needs two successive samples per process to compute CPU%.
    We do a seed pass, sleep briefly, then collect the real values.
    """
    import heapq
    attrs = ["pid", "name", "cpu_percent", "memory_percent", "status"]
    # Seed the per-process CPU counters
    list(psutil.process_iter(attrs))
    time.sleep(0.25)
    # Second pass gives real CPU% readings
    procs: list[dict] = []
    for p in psutil.process_iter(attrs):
        try:
            procs.append(p.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return heapq.nlargest(n, procs, key=lambda x: x.get("cpu_percent") or 0)


# Panel builders

def build_header(timestamp: str) -> Panel:
    uptime_s = int(time.time() - psutil.boot_time())
    h, rem   = divmod(uptime_s, 3600)
    m, s     = divmod(rem, 60)

    title = Text("  Adaptive CLI Diagnostics  ", style="bold white")
    info  = Text.assemble(
        ("Host: ", "label"), (_HOSTNAME,          "metric"), "  |  ",
        ("OS: ",   "label"), (_OS_INFO,           "info"),   "  |  ",
        ("CPU: ",  "label"), (_CPU_NAME,          "muted"),  "  |  ",
        ("Up: ",   "label"), (f"{h}h {m}m {s}s", "metric"), "  |  ",
        ("At: ",   "label"), (timestamp,          "muted"),
    )
    grid = Table.grid(expand=True)
    grid.add_row(Align(title, align="center"))
    grid.add_row(Align(info,  align="center"))
    return Panel(grid, border_style="bright_blue")


def build_alert_panel(
    cpu_m: dict, mem_m: dict, disk_list: list[dict], cfg: dict
) -> Panel | None:
    alerts: list[Text] = []

    # CPU
    cpu_pct = cpu_m["overall"]
    if cpu_m["style"] == "crit":
        alerts.append(Text(f"  [XX]  CPU critical: {cpu_pct:.1f}% >= {cfg['cpu_crit']}%", style="crit"))
    elif cpu_m["style"] == "warn":
        alerts.append(Text(f"  [!!]  CPU warning:  {cpu_pct:.1f}% >= {cfg['cpu_warn']}%", style="warn"))

    # Memory
    mem_pct = mem_m["percent"]
    if mem_m["style"] == "crit":
        alerts.append(Text(f"  [XX]  Memory critical: {mem_pct:.1f}% >= {cfg['mem_crit']}%", style="crit"))
    elif mem_m["style"] == "warn":
        alerts.append(Text(f"  [!!]  Memory warning:  {mem_pct:.1f}% >= {cfg['mem_warn']}%", style="warn"))

    # Disks
    for d in disk_list:
        mp = d["mountpoint"]
        if d["style"] == "crit":
            alerts.append(Text(f"  [XX]  Disk {mp}: {d['percent']:.1f}% >= {cfg['disk_crit']}%", style="crit"))
        elif d["style"] == "warn":
            alerts.append(Text(f"  [!!]  Disk {mp}: {d['percent']:.1f}% >= {cfg['disk_warn']}%", style="warn"))

    if not alerts:
        return None

    grid = Table.grid()
    for a in alerts:
        grid.add_row(a)
    return Panel(grid, title="[bold red]!! Active Alerts[/]", border_style="red")


def build_cpu_panel(m: dict, cfg: dict) -> Panel:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="label", justify="right")
    grid.add_column()
    sty = m["style"]
    cw, cc = cfg["cpu_warn"], cfg["cpu_crit"]

    grid.add_row(
        "Overall",
        Text(f"{m['overall']:5.1f}%  ", style=sty) + gauge_bar(m["overall"], cw, cc),
    )
    if m["freq_current"]:
        grid.add_row("Frequency", Text(fmt_freq(m["freq_current"]), style="metric"))
        grid.add_row("Max Freq",  Text(fmt_freq(m["freq_max"]),     style="muted"))
    grid.add_row("Ctx Switches", Text(f"{m['ctx_switches']:,}", style="muted"))
    grid.add_row("Interrupts",   Text(f"{m['interrupts']:,}",   style="muted"))

    core_line = Text()
    for i, pct in enumerate(m["per_core"]):
        core_line.append(f" C{i}:", style="label")
        core_line.append(f"{pct:4.0f}%", style=severity(pct, cw, cc))
    grid.add_row("Cores", core_line)

    return Panel(grid, title=f"[{sty}]{sev_icon(sty)} CPU[/]", border_style=sty, expand=True)


def build_mem_panel(m: dict, cfg: dict) -> Panel:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="label", justify="right")
    grid.add_column()
    sty    = m["style"]
    mw, mc = cfg["mem_warn"], cfg["mem_crit"]
    sw_sty = severity(m["swap_percent"], mw, mc)

    grid.add_row("RAM Usage",
        Text(f"{m['percent']:5.1f}%  ", style=sty) + gauge_bar(m["percent"], mw, mc))
    grid.add_row("Used / Total",
        Text(f"{fmt_bytes(m['used'])} / {fmt_bytes(m['total'])}", style="metric"))
    grid.add_row("Available",
        Text(fmt_bytes(m["available"]), style="muted"))
    grid.add_row("Swap",
        Text(f"{m['swap_percent']:5.1f}%  ", style=sw_sty) + gauge_bar(m["swap_percent"], mw, mc))
    grid.add_row("Swap Used",
        Text(f"{fmt_bytes(m['swap_used'])} / {fmt_bytes(m['swap_total'])}", style="muted"))

    return Panel(grid, title=f"[{sty}]{sev_icon(sty)} Memory[/]", border_style=sty, expand=True)


def build_disk_panel(disks: list[dict], cfg: dict) -> Panel:
    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    tbl.add_column("Mount",  style="label")
    tbl.add_column("FS")
    tbl.add_column("Used",   justify="right")
    tbl.add_column("Total",  justify="right")
    tbl.add_column("Free",   justify="right")
    tbl.add_column("Usage",  min_width=26)
    dw, dc = cfg["disk_warn"], cfg["disk_crit"]
    for d in disks:
        tbl.add_row(
            d["mountpoint"], d["fstype"],
            fmt_bytes(d["used"]), fmt_bytes(d["total"]), fmt_bytes(d["free"]),
            Text(f"{d['percent']:4.1f}%  ") + gauge_bar(d["percent"], dw, dc),
        )
    overall = worst_severity([d["style"] for d in disks])
    return Panel(tbl, title=f"[{overall}]{sev_icon(overall)} Disks[/]", border_style=overall, expand=True)


def build_net_panel(m: dict) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="label", justify="right"); grid.add_column()
    grid.add_column(style="label", justify="right"); grid.add_column()

    grid.add_row("Sent",       Text(fmt_bytes(m["bytes_sent"]),    style="metric"),
                 "Received",   Text(fmt_bytes(m["bytes_recv"]),    style="metric"))
    grid.add_row("Pkts Out",   Text(f"{m['packets_sent']:,}",      style="muted"),
                 "Pkts In",    Text(f"{m['packets_recv']:,}",      style="muted"))
    grid.add_row("Errors Out", Text(str(m["errout"]), style="crit" if m["errout"] else "ok"),
                 "Errors In",  Text(str(m["errin"]),  style="crit" if m["errin"]  else "ok"))
    grid.add_row("Drops Out",  Text(str(m["dropout"]), style="warn" if m["dropout"] else "ok"),
                 "Drops In",   Text(str(m["dropin"]),  style="warn" if m["dropin"]  else "ok"))

    return Panel(grid, title="[info]Network I/O[/]", border_style="cyan", expand=True)


def build_proc_panel(procs: list[dict]) -> Panel:
    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    tbl.add_column("PID",   justify="right")
    tbl.add_column("Name")
    tbl.add_column("CPU %", justify="right")
    tbl.add_column("MEM %", justify="right")
    tbl.add_column("Status")
    for p in procs:
        cpu = p.get("cpu_percent") or 0.0
        mem = p.get("memory_percent") or 0.0
        tbl.add_row(
            str(p["pid"]),
            (p.get("name") or "?")[:26],
            Text(f"{cpu:.1f}", style=severity(cpu, 50, 80)),
            Text(f"{mem:.1f}", style="metric"),
            Text(p.get("status") or "?", style="muted"),
        )
    return Panel(tbl, title="[info]Top Processes[/]", border_style="cyan", expand=True)


def build_temp_panel(temps: list[dict], cfg: dict) -> Panel | None:
    if not temps:
        return None
    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    tbl.add_column("Sensor")
    tbl.add_column("Label")
    tbl.add_column("Current", justify="right")
    tbl.add_column("High",    justify="right")
    for t in temps:
        tbl.add_row(
            t["sensor"], t["label"],
            Text(f"{t['current']:.1f}C", style=t["style"]),
            Text(f"{t['high']:.1f}C" if t["high"] else "-", style="muted"),
        )
    overall = worst_severity([t["style"] for t in temps])
    return Panel(tbl, title=f"[{overall}]{sev_icon(overall)} Temperatures[/]",
                 border_style=overall, expand=True)


# Snapshot renderer

def render_snapshot(cfg: dict) -> None:
    """
    Collect all metrics concurrently and render one full diagnostics view.
    CPU sampling blocks for ~0.4 s; all other collectors run in parallel
    during that window, keeping total wall time close to the sampling interval.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with ThreadPoolExecutor(max_workers=6) as ex:
        fut_cpu   = ex.submit(get_cpu_metrics, cfg)
        fut_mem   = ex.submit(get_mem_metrics, cfg)
        fut_disk  = ex.submit(get_disk_metrics, cfg)
        fut_net   = ex.submit(get_net_metrics)
        fut_temp  = ex.submit(get_temp_metrics, cfg)
        fut_procs = ex.submit(get_top_processes, cfg["top_procs"])

        cpu_m     = fut_cpu.result()
        mem_m     = fut_mem.result()
        disk_list = fut_disk.result()
        net_m     = fut_net.result()
        temp_list = fut_temp.result()
        procs     = fut_procs.result()

    console.print(build_header(timestamp))

    alert = build_alert_panel(cpu_m, mem_m, disk_list, cfg)
    if alert:
        console.print(alert)

    console.print(Columns([build_cpu_panel(cpu_m, cfg), build_mem_panel(mem_m, cfg)], equal=True))
    console.print(build_disk_panel(disk_list, cfg))
    console.print(build_net_panel(net_m))

    temp_panel = build_temp_panel(temp_list, cfg)
    if temp_panel:
        console.print(temp_panel)

    console.print(build_proc_panel(procs))


# CLI commands

def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[metric]{APP_NAME}[/] [muted]v{APP_VERSION}[/]")
        raise typer.Exit()


@app.callback()
def _cli_root(
    version: bool = typer.Option(
        False, "--version", "-V",
        callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    pass


@app.command()
def diagnose(
    watch:    bool       = typer.Option(False, "--watch",    "-w", help="Refresh continuously."),
    interval: int | None = typer.Option(None,  "--interval", "-i", help="Refresh interval in seconds (with --watch)."),
) -> None:
    """
    Run a full system diagnostics snapshot.

    Add [bold]--watch[/] / [bold]-w[/] for live refresh mode.
    """
    cfg = load_config()
    if interval is not None:
        cfg["interval"] = interval

    if watch:
        console.print(Rule("[cyan]Live Mode -- press Ctrl+C to quit[/]"))
        try:
            while True:
                console.clear()
                render_snapshot(cfg)
                console.print(Rule(f"[muted]Refreshing every {cfg['interval']}s[/]"))
                time.sleep(cfg["interval"])
        except KeyboardInterrupt:
            console.print("\n[info]Diagnostics stopped.[/]")
    else:
        render_snapshot(cfg)


@app.command()
def config(
    reset:     bool       = typer.Option(False, "--reset",     help="Reset all settings to defaults."),
    cpu_warn:  int | None = typer.Option(None,                 help="CPU warning threshold (%%)."),
    cpu_crit:  int | None = typer.Option(None,                 help="CPU critical threshold (%%)."),
    mem_warn:  int | None = typer.Option(None,                 help="Memory warning threshold (%%)."),
    mem_crit:  int | None = typer.Option(None,                 help="Memory critical threshold (%%)."),
    disk_warn: int | None = typer.Option(None,                 help="Disk warning threshold (%%)."),
    disk_crit: int | None = typer.Option(None,                 help="Disk critical threshold (%%)."),
    temp_warn: int | None = typer.Option(None,                 help="Temp warning threshold (C)."),
    temp_crit: int | None = typer.Option(None,                 help="Temp critical threshold (C)."),
    interval:  int | None = typer.Option(None,                 help="Default live-refresh interval (s)."),
    top_procs: int | None = typer.Option(None,                 help="Number of top processes to show."),
) -> None:
    """
    View or update diagnostics thresholds and settings.

    Run with no options to display the current configuration.
    """
    if reset:
        save_config(DEFAULT_CONFIG)
        console.print("[ok]Config reset to defaults.[/]")
        return

    cfg = load_config()
    changed = {k: v for k, v in {
        "cpu_warn":  cpu_warn,  "cpu_crit":  cpu_crit,
        "mem_warn":  mem_warn,  "mem_crit":  mem_crit,
        "disk_warn": disk_warn, "disk_crit": disk_crit,
        "temp_warn": temp_warn, "temp_crit": temp_crit,
        "interval":  interval,  "top_procs": top_procs,
    }.items() if v is not None}

    if changed:
        cfg.update(changed)
        save_config(cfg)
        console.print(f"[ok]Updated {len(changed)} setting(s).[/]")

    _LABELS: tuple[tuple[str, str], ...] = (
        ("cpu_warn",  "CPU Warning (%)"),
        ("cpu_crit",  "CPU Critical (%)"),
        ("mem_warn",  "Memory Warning (%)"),
        ("mem_crit",  "Memory Critical (%)"),
        ("disk_warn", "Disk Warning (%)"),
        ("disk_crit", "Disk Critical (%)"),
        ("temp_warn", "Temp Warning (C)"),
        ("temp_crit", "Temp Critical (C)"),
        ("interval",  "Live Interval (s)"),
        ("top_procs", "Top Processes"),
    )
    tbl = Table(title="Current Configuration", box=box.ROUNDED,
                header_style="bold cyan", show_lines=True, expand=False)
    tbl.add_column("Setting", style="label")
    tbl.add_column("Value",   justify="right")
    tbl.add_column("Default", justify="right", style="muted")

    for key, label in _LABELS:
        val     = cfg.get(key, DEFAULT_CONFIG[key])
        default = DEFAULT_CONFIG[key]
        style   = "bold yellow" if val != default else "metric"
        tbl.add_row(label, Text(str(val), style=style), str(default))

    console.print(tbl)
    console.print(f"[muted]Config file: {CONFIG_FILE}[/]")


@app.command()
def report(
    output: Path = typer.Argument(..., help="Output path for the JSON report (e.g. report.json)."),
) -> None:
    """Export a full diagnostics snapshot to a JSON file."""
    cfg = load_config()
    with console.status("[cyan]Collecting metrics\u2026[/]", spinner="dots"):
        with ThreadPoolExecutor(max_workers=6) as ex:
            f_cpu   = ex.submit(get_cpu_metrics, cfg)
            f_mem   = ex.submit(get_mem_metrics, cfg)
            f_disk  = ex.submit(get_disk_metrics, cfg)
            f_net   = ex.submit(get_net_metrics)
            f_temp  = ex.submit(get_temp_metrics, cfg)
            f_procs = ex.submit(get_top_processes, cfg["top_procs"])
            data = {
                "timestamp":     datetime.now().isoformat(),
                "host":          _HOSTNAME,
                "os":            _OS_INFO,
                "cpu":           f_cpu.result(),
                "memory":        f_mem.result(),
                "disks":         f_disk.result(),
                "network":       f_net.result(),
                "temperatures":  f_temp.result(),
                "top_processes": f_procs.result(),
                "config_used":   cfg,
            }

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    console.print(f"[ok]Report saved ->[/] [metric]{output.resolve()}[/]")


@app.command()
def top(
    n:     int  = typer.Option(10,    "--count", "-n", help="Number of processes to show."),
    sort:  str  = typer.Option("cpu", "--sort",  "-s", help="Sort by: cpu | mem."),
    watch: bool = typer.Option(False, "--watch", "-w", help="Refresh continuously."),
) -> None:
    """Show the top resource-consuming processes."""
    cfg      = load_config()
    attrs    = ["pid", "name", "cpu_percent", "memory_percent", "status", "username"]
    sort_key = "memory_percent" if sort == "mem" else "cpu_percent"

    def _render() -> Panel:
        import heapq
        # Seed counters, wait, then collect real CPU% readings
        list(psutil.process_iter(attrs))
        time.sleep(0.25)
        procs: list[dict] = []
        for p in psutil.process_iter(attrs):
            try:
                procs.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        top_n = heapq.nlargest(n, procs, key=lambda x: x.get(sort_key) or 0)

        tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan", expand=True)
        tbl.add_column("PID",   justify="right")
        tbl.add_column("Name")
        tbl.add_column("User")
        tbl.add_column("CPU %", justify="right")
        tbl.add_column("MEM %", justify="right")
        tbl.add_column("Status")

        for p in top_n:
            cpu_v = p.get("cpu_percent")    or 0.0
            mem_v = p.get("memory_percent") or 0.0
            tbl.add_row(
                str(p["pid"]),
                (p.get("name")     or "?")[:30],
                (p.get("username") or "?")[:15],
                Text(f"{cpu_v:.1f}", style=severity(cpu_v, 50, 80)),
                Text(f"{mem_v:.1f}", style="metric"),
                Text(p.get("status") or "?", style="muted"),
            )
        ts = datetime.now().strftime("%H:%M:%S")
        return Panel(
            tbl,
            title=f"[info]Top {n} Processes -- sorted by {sort.upper()} [{ts}][/]",
            border_style="cyan",
        )

    if watch:
        console.print(Rule("[cyan]Live Process View -- press Ctrl+C to quit[/]"))
        try:
            while True:
                console.clear()
                console.print(_render())
                console.print(Rule(f"[muted]Refreshing every {cfg['interval']}s[/]"))
                time.sleep(cfg["interval"])
        except KeyboardInterrupt:
            console.print("\n[info]Stopped.[/]")
    else:
        console.print(_render())


@app.command()
def ping_check(
    hosts:   list[str] = typer.Argument(..., help="Hostnames or IPs to ping."),
    timeout: int       = typer.Option(3, "--timeout", "-t", help="Per-host timeout in seconds."),
    count:   int       = typer.Option(1, "--count",   "-c", help="Number of packets to send per host."),
) -> None:
    """
    Check reachability of hosts via ICMP ping.

    All hosts are pinged concurrently for fast results even with large lists.
    """
    param   = "-n" if _IS_WIN else "-c"
    _lat_re = re.compile(r"time[<=]?([\d.]+)\s*ms", re.IGNORECASE)

    def _ping(host: str) -> tuple[str, str, str, str]:
        """Returns (host, status, latency, severity_style)."""
        try:
            res = subprocess.run(
                ["ping", param, str(count), host],
                capture_output=True, text=True, timeout=timeout,
            )
            if res.returncode == 0:
                m = _lat_re.search(res.stdout)
                latency = f"{m.group(1)} ms" if m else "< 1 ms"
                return host, "Reachable", latency, "ok"
            return host, "Unreachable", "-", "crit"
        except subprocess.TimeoutExpired:
            return host, "Timeout", "-", "warn"
        except Exception as exc:
            return host, f"Error: {exc}", "-", "crit"

    results: dict[str, tuple[str, str, str]] = {}
    # Cap workers to avoid spawning hundreds of threads for huge host lists
    max_workers = min(len(hosts), 32)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_ping, h): h for h in hosts}
        for fut in as_completed(futs):
            host, status, latency, sty = fut.result()
            results[host] = (status, latency, sty)

    tbl = Table(box=box.ROUNDED, header_style="bold cyan", expand=False)
    tbl.add_column("Host")
    tbl.add_column("Status",  justify="center")
    tbl.add_column("Latency", justify="right")

    for host in hosts:
        status, latency, sty = results[host]
        tbl.add_row(
            host,
            Text(f"{sev_icon(sty)}  {status}", style=sty),
            Text(latency, style="metric" if sty == "ok" else "muted"),
        )

    reachable = sum(1 for s, _, __ in results.values() if s == "Reachable")
    console.print(Panel(
        tbl,
        title=f"[info]Ping Check -- {reachable}/{len(hosts)} reachable[/]",
        border_style="cyan",
    ))


# Entry point

if __name__ == "__main__":
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[info]Bye![/]")
