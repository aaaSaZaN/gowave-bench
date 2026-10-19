#!/usr/bin/env python3
"""Render measured benchmark results as standalone SVG figures with Matplotlib."""
import json
import math
import os
from pathlib import Path
import statistics
import tempfile


def plotting():
    # Keep font-cache writes out of read-only home directories and isolate rendering from GUI backends.
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "gowave-benchmark-matplotlib"))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise RuntimeError("SVG reports require Matplotlib: python3 -m pip install -r benchmarks/config/requirements.txt") from None
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "svg.fonttype": "none",
                         "svg.hashsalt": "gowave-benchmark", "axes.spines.top": False,
                         "axes.spines.right": False, "axes.titleweight": "bold",
                         "axes.titlepad": 14, "axes.labelcolor": "#42536a", "text.color": "#172c45",
                         "xtick.color": "#42536a", "ytick.color": "#42536a", "figure.facecolor": "#ffffff"})
    return plt


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def median(values):
    values = [v for v in values if finite(v)]
    return statistics.median(values) if values else None


def complete(rows, repetitions):
    return len(rows) == repetitions and {r["repetition"] for r in rows} == set(range(1, repetitions + 1))


def summarize(records, metadata):
    """Never merge workload/mode/level groups or turn missing measurements into zero."""
    repetitions = metadata.get("load", {}).get("repetitions", 1)
    targets = sorted({r["target"] for r in records if r["mode"] != "smoke"} | set(metadata.get("targets", {})))
    groups = []
    for workload, mode in sorted({(r["workload"], r["mode"]) for r in records if r["mode"] != "smoke"}):
        series = {}
        for target in targets:
            rows = [r for r in records if (r["target"], r["workload"], r["mode"]) == (target, workload, mode)]
            points = []
            for level in sorted({r["level"] for r in rows}):
                trials = [r for r in rows if r["level"] == level]
                valid_memory = [r["memory"] for r in trials if r.get("memory", {}).get("valid")]
                memory_complete = len(valid_memory) == len(trials) and complete(trials, repetitions)
                values = [r.get("p99_ms") for r in trials if finite(r.get("p99_ms"))]
                throughput = [r["successful_rps"] for r in trials if finite(r.get("successful_rps"))]
                operations = {}
                for action in sorted({action for trial in trials for action in trial.get("operations", {})}):
                    samples = [trial.get("operations", {}).get(action, {}) for trial in trials]
                    action_values = [sample.get("p99_ms") for sample in samples if finite(sample.get("p99_ms"))]
                    if len(action_values) == len(trials) and complete(trials, repetitions):
                        operations[action] = {"p99_ms": median(action_values), "min": min(action_values), "max": max(action_values),
                                              "samples": sum(sample.get("samples") or 0 for sample in samples)}
                points.append({"level": level, "repetitions": len(trials),
                               "complete": complete(trials, repetitions),
                               "passed": complete(trials, repetitions) and all(r["passed"] for r in trials),
                               "successful_rps": median(throughput),
                               "rps_min": min(throughput) if throughput else None,
                               "rps_max": max(throughput) if throughput else None,
                               "p99_ms": median(values), "p99_min": min(values) if values else None,
                               "p99_max": max(values) if values else None,
                               "error_percent": median([100 * r["error_rate"] for r in trials if finite(r.get("error_rate"))]),
                               "dropped_iterations": max((r.get("dropped_iterations", 0) for r in trials), default=0),
                               "average_mib": median([m["time_weighted_average_bytes"] / 2**20 for m in valid_memory
                                                      if finite(m.get("time_weighted_average_bytes"))]) if memory_complete else None,
                               "peak_mib": max((m["sampled_peak_bytes"] / 2**20 for m in valid_memory
                                                if finite(m.get("sampled_peak_bytes"))), default=None) if memory_complete else None,
                               "memory_methods": sorted({m["description"] for m in valid_memory}),
                               "operations": operations})
            series[target] = points
        level_sets = [{p["level"] for p in points if p["passed"]} for points in series.values()]
        common = set.intersection(*level_sets) if level_sets else set()
        common_level = max(common) if common else None
        common_points = {target: next((p for p in points if p["level"] == common_level), None)
                         for target, points in series.items()}
        methods = {method for p in common_points.values() if p for method in p["memory_methods"]}
        groups.append({"workload": workload, "mode": mode, "series": series, "common_level": common_level,
                       "common_points": common_points, "memory_comparable": len(methods) <= 1,
                       "best": {target: max((p for p in points if p["passed"]), key=lambda p: p["level"], default=None)
                                for target, points in series.items()},
                       "observed_throughput": {
                           target: max((p for p in points if p["passed"]), key=lambda p: p["successful_rps"] or 0, default=None)
                           for target, points in series.items()} if mode in ("concurrency", "churn") else {}})
    return {"targets": targets, "groups": groups, "expected_repetitions": repetitions,
            "smoke_only": bool(records) and not groups}


def palette(targets):
    colors = ["#007c91", "#c55221", "#7561a8", "#468649", "#c28b10", "#9d4774"]
    return {target: colors[i % len(colors)] for i, target in enumerate(targets)}


def style(ax, ylabel=None):
    ax.set_axisbelow(True)
    ax.grid(axis="y", color="#e5eaf0", linewidth=0.7)
    ax.set_ylim(bottom=0)
    if ylabel:
        ax.set_ylabel(ylabel)


def save_figure(plt, figure, path, description):
    figure.savefig(path, format="svg", bbox_inches="tight",
                   metadata={"Title": path.stem, "Description": description, "Date": None})
    plt.close(figure)


def bars(ax, targets, values, colors, title, unit, note=None, show_values=True):
    ax.set_title(title, loc="left")
    style(ax, unit)
    ax.set_xticks(range(len(targets)), targets)
    present = [value for value in values if finite(value)]
    top = max(present, default=1) * 1.3 or 1
    ax.set_ylim(0, top)
    for i, (target, value) in enumerate(zip(targets, values)):
        if finite(value):
            ax.bar(i, value, color=colors[target], width=0.55)
            if show_values:
                ax.annotate(f"{value:,.1f}", (i, value), xytext=(0, 5), textcoords="offset points", ha="center", fontsize=10)
        else:
            ax.text(i, top * 0.05, "Unavailable", ha="center", fontsize=9, color="#64748b")
    if note:
        ax.text(0, -0.23, note, transform=ax.transAxes, fontsize=8, color="#64748b", va="top", wrap=True)


def endpoints(ax, group, targets, colors, heading):
    points = group["common_points"]
    actions = sorted({action for point in points.values() if point for action in point["operations"]},
                     key=lambda action: max((p["operations"][action]["p99_ms"] for p in points.values()
                                             if p and action in p["operations"]), default=0), reverse=True)
    ax.set_title(f"{heading}\nEndpoint tail latency at equal load ↓", loc="left")
    ax.set_xlabel("Median per-run p99 (ms)")
    ax.grid(axis="x", color="#e5eaf0", linewidth=0.7)
    ax.set_axisbelow(True)
    if not actions:
        ax.text(0.5, 0.5, "No common passing load or\nper-operation measurements", ha="center", transform=ax.transAxes)
        ax.set_yticks([])
        return
    width = 0.7 / max(1, len(targets))
    maximum = 0
    for ti, target in enumerate(targets):
        point = points[target]
        for ai, action in enumerate(actions):
            value = point["operations"].get(action) if point else None
            y = ai - 0.35 + width * (ti + 0.5)
            if value:
                x = value["p99_ms"]
                maximum = max(maximum, value["max"])
                ax.barh(y, x, height=width * 0.9, color=colors[target], label=target if ai == 0 else None,
                        xerr=[[max(0, x - value["min"])], [max(0, value["max"] - x)]],
                        error_kw={"elinewidth": 0.8, "ecolor": "#42536a"})
                ax.annotate(f"{x:.1f}", (x, y), xytext=(5, 0), textcoords="offset points", va="center", fontsize=8)
    labels = {"stats": "Dashboard stats", "list": "User list", "detail": "User detail", "nodes": "Nodes",
              "json": "Xray JSON", "mihomo": "Mihomo", "singbox": "sing-box", "update": "User update"}
    ax.set_yticks(range(len(actions)), [labels.get(a, a) for a in actions], fontsize=8)
    ax.invert_yaxis()
    ax.set_xlim(0, maximum * 1.3 or 1)
    ax.legend(fontsize=8, loc="best")
    ax.text(0, -0.28, "Fixed-rate run: capacity is not measured.\nWhiskers show the per-run range.", transform=ax.transAxes,
            fontsize=8, color="#64748b", va="top")


def overview(plt, data, startup, colors, path, title):
    groups = data["groups"]
    startup_targets = sorted({r["target"] for r in startup})
    nrows = max(1, len(groups) + bool(startup_targets))
    height = 3.8 * nrows + 1.8
    figure, axes = plt.subplots(nrows, 3, figsize=(15, height), squeeze=False)
    # Margins in inches: the title must clear the two-line panel headings.
    figure.subplots_adjust(top=1 - 1.85 / height, bottom=0.12 / nrows,
                           hspace=0.85, wspace=0.34)
    figure.suptitle(title, x=0.05, y=1 - 0.3 / height, ha="left", va="top", fontsize=20, fontweight="bold")
    for index, group in enumerate(groups):
        targets = data["targets"]
        heading = f"{group['workload']} / {group['mode']}"
        if group["mode"] == "rate":
            endpoints(axes[index][0], group, targets, colors, heading)
        else:
            best = [group["observed_throughput"].get(target) for target in targets]
            levels = ", ".join(f"{target}: {p['level']} VUs" for target, p in zip(targets, best) if p)
            bars(axes[index][0], targets, [p["successful_rps"] if p else None for p in best], colors,
                 f"{heading}\nHighest observed passing throughput ↑", "Successful req/s",
                 f"{levels}\nBest observed median; saturation is not guaranteed.")
        common = group["common_level"]
        comparison_basis = "offered load" if group["mode"] == "rate" else "client count"
        context = f"Common passing load: {common} {'req/s' if group['mode'] == 'rate' else 'VUs'}" if common is not None else "No common fully passing load was measured."
        points = [group["common_points"][target] for target in targets]
        bars(axes[index][1], targets, [p["p99_ms"] if p else None for p in points], colors,
             f"Tail latency at equal {comparison_basis} ↓", "Median per-run p99 (ms)", context)
        ax = axes[index][2]
        bars(ax, targets, [p["average_mib"] if p and group["memory_comparable"] else None for p in points],
             colors, f"Memory at equal {comparison_basis} ↓", "MiB", context if group["memory_comparable"] else "Memory methods/scopes differ; comparison withheld.", show_values=False)
        peaks = [(i, p["peak_mib"]) for i, p in enumerate(points) if p and group["memory_comparable"] and finite(p["peak_mib"])]
        if peaks:
            peak_max = max(value for _, value in peaks)
            ax.set_ylim(top=max(ax.get_ylim()[1], peak_max * 1.35))
            for i, value in peaks:
                ax.scatter(i, value, marker="_", s=250, color="#172c45", zorder=5)
                average = points[i]["average_mib"]
                label = f"avg {average:,.1f}\npeak {value:,.1f}" if finite(average) else f"peak {value:,.1f}"
                ax.annotate(label, (i, value), xytext=(0, 8), textcoords="offset points", ha="center", fontsize=8)
    if startup_targets:
        axrow = axes[len(groups)]
        for ax, key, label in [(axrow[0], "tcp_accept_ms", "TCP acceptance ↓"),
                               (axrow[1], "api_ready_ms", "Authenticated API readiness ↓")]:
            values = [median([r.get(key) for r in startup if r["target"] == t and not r.get("failed")]) for t in startup_targets]
            bars(ax, startup_targets, values, colors, f"Container/application startup\n{label}", "Median milliseconds")
            for i, target in enumerate(startup_targets):
                values = [r.get(key) for r in startup if r["target"] == target and not r.get("failed") and finite(r.get(key))]
                ax.scatter([i] * len(values), values, color="#172c45", alpha=0.7, s=16, zorder=4)
                if values:
                    ax.set_ylim(top=max(ax.get_ylim()[1], max(values) * 1.15))
        axrow[2].axis("off")
        failures = sum(bool(r.get("failed")) for r in startup)
        axrow[2].text(0, 0.8, f"Startup: {len(startup)} recorded trials\n{failures} failed trials\n\nLaunch-command overhead is included.\nDatabases and caches remain running.\nDots show individual successful trials.", va="top", linespacing=1.7)
    if not groups and not startup_targets:
        for ax in axes[0]:
            ax.axis("off")
        axes[0][1].text(0.5, 0.5, "Smoke validation only\nNo comparative performance estimate", ha="center", va="center", fontsize=15)
    figure.text(0.05, 0.01, "Measured results only • missing/incomplete measurements are not zero • separate workloads and load levels", fontsize=9, color="#64748b")
    save_figure(plt, figure, path, "Throughput, equal-load latency/memory and startup comparison; arrows indicate preferred direction.")


def curves(plt, group, metadata, colors, path):
    figure, axes = plt.subplots(2, 2, figsize=(13, 9))
    figure.subplots_adjust(top=0.87, bottom=0.16, hspace=0.55, wspace=0.27)
    figure.suptitle(f"{group['workload']} / {group['mode']} — load response", x=0.07, ha="left", fontsize=20, fontweight="bold")
    units = "Offered requests / second" if group["mode"] == "rate" else "Active virtual users"
    for ax in axes.flat:
        style(ax)
        ax.set_xlabel(units)
    axes[0][0].set_title("Successful throughput ↑", loc="left")
    axes[0][0].set_ylabel("Successful req/s")
    axes[0][1].set_title("Tail latency ↓", loc="left")
    axes[0][1].set_ylabel("Per-run p99 (ms)")
    axes[1][0].set_title("Invalid / failed responses ↓", loc="left")
    axes[1][0].set_ylabel("Errors (%)")
    axes[1][1].set_title("Memory: average and sampled peak ↓", loc="left")
    axes[1][1].set_ylabel("MiB")
    for target, points in group["series"].items():
        color = colors[target]
        for ax, key, low, high in [(axes[0][0], "successful_rps", "rps_min", "rps_max"),
                                   (axes[0][1], "p99_ms", "p99_min", "p99_max")]:
            values = [p for p in points if finite(p[key])]
            ax.plot([p["level"] for p in values], [p[key] for p in values], color=color, label=target, linewidth=2)
            for p in values:
                marker = "o" if p["passed"] else "x"
                ax.scatter(p["level"], p[key], color=color, marker=marker, s=45, zorder=4)
                if finite(p[low]) and finite(p[high]):
                    ax.vlines(p["level"], p[low], p[high], color=color, alpha=0.5, linewidth=4)
        values = [p for p in points if finite(p["error_percent"])]
        axes[1][0].plot([p["level"] for p in values], [p["error_percent"] for p in values], marker="o", color=color, label=target)
        for p in values:
            if p["dropped_iterations"]:
                axes[1][0].annotate(f"{p['dropped_iterations']} drops", (p["level"], p["error_percent"]),
                                     xytext=(0, 10), textcoords="offset points", ha="center", fontsize=8, color=color)
        for key, linestyle, label in [("average_mib", "-", "average"), ("peak_mib", "--", "peak")]:
            # NaNs intentionally break lines where a memory measurement is missing.
            axes[1][1].plot([p["level"] for p in points], [p[key] if finite(p[key]) else float("nan") for p in points],
                            color=color, linestyle=linestyle, marker="o", label=f"{target} {label}")
    load = metadata.get("load", {})
    for ax, key, factor, label in [(axes[0][1], "p99_ms", 1, "p99 SLO"),
                                   (axes[1][0], "max_error_rate", 100, "Error SLO")]:
        if finite(load.get(key)):
            ax.axhline(load[key] * factor, color="#8190a5", linestyle=":", label=label)
    for ax in axes.flat:
        # style() fixes the lower bound before data exist; re-enable scaling after plotting.
        ax.autoscale(enable=True, axis="y")
        ax.set_ylim(bottom=0)
        ax.legend(fontsize=8, loc="best")
    if not any(finite(p["average_mib"]) for points in group["series"].values() for p in points):
        axes[1][1].text(0.5, 0.5, "Memory measurements unavailable", ha="center", transform=axes[1][1].transAxes)
    figure.text(0.07, 0.05, "Line: median across repeated runs; whiskers: min–max. Latency is median of per-run p99s, not a pooled percentile.\nCircle: all repetitions pass. Cross: failed or incomplete level. Missing memory is omitted; peaks are sampled.",
                fontsize=9, color="#64748b", linespacing=1.6)
    save_figure(plt, figure, path, "Separate steady-load points with per-run ranges, SLO failures and missing memory explicitly indicated.")


def render(directory, records=None, metadata=None, startup=None, title="Gowave / Remnawave — benchmark comparison"):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    plt = plotting()
    records, metadata, startup = records or [], metadata or {}, startup or []
    data = summarize(records, metadata)
    colors = palette(sorted(set(data["targets"]) | {r["target"] for r in startup}))
    overview(plt, data, startup, colors, directory / "comparison.svg", title)
    files = ["comparison.svg"]
    for group in data["groups"]:
        path = directory / f"{group['workload']}-{group['mode']}.svg"
        curves(plt, group, metadata, colors, path)
        files.append(path.name)
    (directory / "charts.json").write_text(json.dumps({**data, "startup": startup}, indent=2, allow_nan=False) + "\n")
    return files


def add_commands(sub):
    parser = sub.add_parser("figures", help="Combine compatible load/connection/startup results into SVG images")
    parser.add_argument("--load", type=Path, help="One load result directory")
    parser.add_argument("--connections", type=Path, help="Matching concurrent-client/churn result directory")
    parser.add_argument("--startup", type=Path, help="One startup result directory")
    parser.add_argument("--output", required=True, type=Path)


def combined_loads(paths):
    from . import engine
    records, metadata = [], None
    for path in paths:
        current = json.loads((path / "metadata.json").read_text())
        if metadata is not None:
            for key in ("dataset_hash", "targets"):
                if current.get(key) != metadata.get(key):
                    raise ValueError(f"Cannot combine load results with different {key}")
            for key in ("repetitions", "duration_seconds", "p99_ms", "max_error_rate", "min_completion_ratio"):
                if current.get("load", {}).get(key) != metadata.get("load", {}).get(key):
                    raise ValueError(f"Cannot combine load results with different load.{key}")
        else:
            metadata = current
        records.extend(engine.read_records(path))
    keys = [(r["target"], r["workload"], r["mode"], r["level"], r["repetition"]) for r in records]
    if len(keys) != len(set(keys)):
        raise ValueError("Combined load results contain duplicate trials")
    return records, metadata or {}


def execute(args):
    if not args.load and not args.startup and not args.connections:
        raise ValueError("Provide --load, --connections and/or --startup")
    records, metadata = combined_loads([p for p in (args.load, args.connections) if p])
    startup = json.loads((args.startup / "startup.json").read_text()) if args.startup else []
    render(args.output, records, metadata, startup,
           title=metadata.get("experiment_label") or "Gowave / Remnawave — benchmark comparison")
    print(f"SVG figures: {args.output}")
    return 0
