import json
import itertools
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import engine as bench


class ScoringTests(unittest.TestCase):
    def summary(self, **overrides):
        metrics = {
            "bench_requests": {"count": 100, "rate": 10},
            "bench_successes": {"count": 100, "rate": 10},
            "bench_errors": {"rate": 0},
            "bench_latency": {"med": 10, "p(95)": 20, "p(99)": 30},
            "dropped_iterations": {"count": 0}
        }
        metrics.update(overrides)
        return {"metrics": {key: {"values": value} for key, value in metrics.items()}}

    def score(self, summary, code=0):
        return bench.score(summary, "rate", 10, 10, bench.DEFAULT_LOAD, code)

    def test_dropped_load_is_not_capacity(self):
        self.assertTrue(self.score(self.summary())["passed"])
        self.assertFalse(self.score(self.summary(dropped_iterations={"count": 1}))["passed"])

    def test_under_delivery_with_no_drops_is_not_capacity(self):
        self.assertFalse(self.score(self.summary(bench_requests={"count": 90}))["passed"])

    def test_fast_errors_do_not_count_as_successful_throughput(self):
        result = self.score(self.summary(bench_successes={"count": 20, "rate": 2}, bench_errors={"rate": 0.8}))
        self.assertEqual(result["successful_rps"], 2)
        self.assertFalse(result["passed"])

    def test_missing_summary_is_a_failure(self):
        self.assertFalse(self.score({})["passed"])

    def test_operation_metrics_preserve_endpoint_percentiles(self):
        summary = self.summary()
        summary["metrics"]["bench_latency{action:stats}"] = {"values": {"p(99)": 34, "p(95)": 29, "med": 24, "count": 100}}
        operations = self.score(summary)["operations"]
        self.assertEqual(operations["stats"], {"p50_ms": 24, "p95_ms": 29, "p99_ms": 34, "samples": 100})

    def test_threshold_and_process_failures_are_preserved(self):
        self.assertFalse(self.score(self.summary(bench_latency={"p(99)": 501}))["passed"])
        self.assertFalse(self.score(self.summary(), 99)["passed"])

    def test_report_requires_every_repetition_to_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            rows = []
            for level, rep, passed, rps in [(10, 1, True, 10), (20, 1, True, 20), (20, 2, False, 19)]:
                rows.append({"target": "go", "workload": "reads", "mode": "rate", "level": level,
                             "repetition": rep, "memory": bench.memory_summary([], 0, "disabled"),
                             **self.score(self.summary()), "passed": passed, "successful_rps": rps})
            bench.save(Path(temp) / "results.json", rows)
            bench.report(Path(temp))
            report = (Path(temp) / "report.md").read_text()
            self.assertIn("| go | reads | rate | 20.00 | 10 | 10.00 |", report)

    def test_partial_report_cannot_claim_capacity_with_missing_repetitions(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            row = {"target": "go", "workload": "reads", "mode": "rate", "level": 10,
                   "repetition": 1, "memory": bench.memory_summary([], 0, "disabled"),
                   **self.score(self.summary())}
            bench.save(directory / "results.json", [row])
            bench.save(directory / "metadata.json", {"load": {"repetitions": 3}})
            bench.report(directory)
            self.assertIn("| go | reads | rate | 10.00 | none | n/a |", (directory / "report.md").read_text())


class MemoryTests(unittest.TestCase):
    def test_average_is_weighted_by_time(self):
        result = bench.memory_summary([(0, 10), (1, 20), (4, 40)], 0, "test")
        self.assertEqual(result["time_weighted_average_bytes"], 26.25)
        self.assertEqual(result["sampled_peak_bytes"], 40)

    def test_failed_samples_invalidate_memory(self):
        self.assertFalse(bench.memory_summary([(0, 10), (1, 20)], 1, "test")["valid"])
        self.assertFalse(bench.memory_summary([], 0, "disabled")["valid"])

    def test_pid_forest_includes_children_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for pid, parent, pages in [(10, 1, 10), (11, 10, 20), (12, 11, 30), (99, 1, 99)]:
                (root / str(pid)).mkdir()
                fields = ["S", str(parent)] + ["0"] * 19 + [str(pages)]
                (root / str(pid) / "stat").write_text(f"{pid} (name with ) parentheses) " + " ".join(fields))
            self.assertEqual(bench.process_rss([10, 11], root), 60 * os.sysconf("SC_PAGE_SIZE"))
            with self.assertRaises(RuntimeError):
                bench.process_rss([404], root)

    def test_cgroups_cannot_overlap(self):
        with self.assertRaises(ValueError):
            bench.memory_reader({"memory": {"kind": "cgroup", "paths": ["/sys/fs/cgroup/a", "/sys/fs/cgroup/a/b"]}})


class ConfigurationTests(unittest.TestCase):
    def test_example_is_valid(self):
        config = bench.load_config(bench.ROOT / "config/example.json")
        self.assertEqual(config["dataset"]["users"], 1000)

    def test_startup_command_cannot_be_shell_text(self):
        with self.assertRaises(ValueError):
            bench.command_args("docker stop example && docker start example")

    def test_public_entry_point_and_documentation_are_consolidated(self):
        self.assertEqual(sorted(p.name for p in bench.ROOT.glob("*.py")), ["bench.py"])
        self.assertFalse((bench.ROOT / "README.md").exists())
        self.assertFalse((bench.ROOT / "docker/README.md").exists())
        self.assertTrue((bench.ROOT / "docs/README.md").is_file())
        self.assertFalse((bench.ROOT / "docker/lab.py").exists())


class StartupTests(unittest.TestCase):
    def test_tcp_acceptance_alone_does_not_establish_readiness_and_failure_stops_target(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "results"
            args = SimpleNamespace(targets=None, output=str(output), repetitions=1)
            target = {"url": "http://localhost:3000", "startup": {
                "start": ["start-benchmark"], "stop": ["stop-benchmark"],
                "timeout_seconds": 0.02, "poll_interval_seconds": 0.001}}
            with patch.object(bench, "authenticate", return_value="private-token"), \
                    patch.object(bench.platform, "platform", return_value="test-platform"), \
                    patch.object(bench.time, "monotonic", side_effect=itertools.count(0, 0.001)), \
                    patch.object(bench.time, "sleep"), \
                    patch.object(bench.subprocess, "run") as stop, \
                    patch.object(bench.subprocess, "Popen") as start, \
                    patch.object(bench, "stop_process") as cleanup, \
                    patch.object(bench, "tcp_accepts", side_effect=[False, True]), \
                    patch.object(bench, "api", side_effect=RuntimeError("not ready")):
                start.return_value.poll.return_value = None
                with self.assertRaisesRegex(RuntimeError, "readiness timed out"):
                    bench.run_startup(args, {"targets": {"test": target}})
                self.assertEqual(stop.call_count, 2)
                cleanup.assert_called_once_with(start.return_value)
            trial = json.loads((output / "startup.json").read_text())[0]
            self.assertTrue(trial["failed"])
            self.assertIsNotNone(trial["tcp_accept_ms"])
            self.assertIsNone(trial["api_ready_ms"])
            self.assertNotIn("private-token", (output / "test-metadata.json").read_text())


if __name__ == "__main__":
    unittest.main()
