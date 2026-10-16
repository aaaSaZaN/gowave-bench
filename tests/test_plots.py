import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import plots
from lib import containers as lab


def trial(target, level, rep, passed=True, latency=50, memory=True, description="cgroup app"):
    return {"target": target, "workload": "reads", "mode": "rate", "level": level,
            "repetition": rep, "passed": passed, "successful_rps": level,
            "p99_ms": latency, "error_rate": 0 if passed else 0.5, "dropped_iterations": 0,
            "memory": {"valid": memory, "description": description,
                       "time_weighted_average_bytes": 64 * 2**20, "sampled_peak_bytes": 80 * 2**20}}


class ChartDataTests(unittest.TestCase):
    def test_common_load_excludes_a_level_with_one_failed_repetition(self):
        rows = [trial(target, level, rep, passed=not (target == "remnawave" and level == 20 and rep == 2))
                for target in ("gowave", "remnawave") for level in (10, 20) for rep in (1, 2, 3)]
        group = plots.summarize(rows, {"load": {"repetitions": 3}})["groups"][0]
        self.assertEqual(group["common_level"], 10)
        self.assertEqual(group["best"]["gowave"]["level"], 20)
        self.assertEqual(group["best"]["remnawave"]["level"], 10)

    def test_missing_expected_target_withholds_equal_load_comparison(self):
        data = plots.summarize([trial("gowave", 10, 1)], {"targets": {"gowave": {}, "remnawave": {}}})
        self.assertIsNone(data["groups"][0]["common_level"])

    def test_missing_memory_is_not_zero_and_different_scopes_are_not_compared(self):
        data = plots.summarize([trial("go", 10, 1, memory=False), trial("ts", 10, 1)], {})
        self.assertIsNone(data["groups"][0]["common_points"]["go"]["average_mib"])
        data = plots.summarize([trial("go", 10, 1), trial("ts", 10, 1, description="cgroup stack")], {})
        self.assertFalse(data["groups"][0]["memory_comparable"])

    def test_incomplete_repetitions_cannot_claim_capacity(self):
        data = plots.summarize([trial("go", 10, 1)], {"load": {"repetitions": 3}})
        self.assertIsNone(data["groups"][0]["best"]["go"])

    def test_latency_is_median_of_per_run_p99_and_preserves_range(self):
        rows = [trial("go", 10, rep, latency=value) for rep, value in enumerate([10, 20, 100], 1)]
        point = plots.summarize(rows, {"load": {"repetitions": 3}})["groups"][0]["series"]["go"][0]
        self.assertEqual(point["p99_ms"], 20)
        self.assertEqual((point["p99_min"], point["p99_max"]), (10, 100))

    def test_smoke_results_do_not_estimate_performance(self):
        row = {**trial("go", 1, 1), "mode": "smoke"}
        data = plots.summarize([row], {})
        self.assertTrue(data["smoke_only"])
        self.assertEqual(data["groups"], [])

    def test_rate_capped_results_do_not_claim_throughput_capacity(self):
        group = plots.summarize([trial("go", 100, 1), trial("ts", 100, 1)], {})["groups"][0]
        self.assertEqual(group["observed_throughput"], {})

    def test_observed_throughput_uses_fastest_passing_vu_level(self):
        rows = []
        for vus, rps, passed in [(2, 300, True), (10, 1000, True), (50, 900, True), (100, 1500, False)]:
            row = {**trial("go", vus, 1, passed=passed), "mode": "concurrency", "successful_rps": rps}
            rows.append(row)
        group = plots.summarize(rows, {})["groups"][0]
        self.assertEqual(group["observed_throughput"]["go"]["level"], 10)
        self.assertEqual(group["observed_throughput"]["go"]["successful_rps"], 1000)

    def test_endpoint_breakdown_does_not_replace_each_action_with_mixed_p99(self):
        row = trial("go", 100, 1, latency=30)
        row["operations"] = {"detail": {"p99_ms": 2, "samples": 200}, "stats": {"p99_ms": 34, "samples": 100}}
        point = plots.summarize([row], {})["groups"][0]["common_points"]["go"]
        self.assertEqual(point["operations"]["detail"]["p99_ms"], 2)
        self.assertEqual(point["operations"]["stats"]["p99_ms"], 34)
        self.assertEqual(point["p99_ms"], 30)

    def test_combining_different_deployments_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp) / name for name in ["rate", "connections"]]
            for path, identity in zip(paths, ["a", "b"]):
                path.mkdir()
                (path / "results.json").write_text("[]")
                (path / "metadata.json").write_text(json.dumps({"dataset_hash": identity}))
            with self.assertRaisesRegex(ValueError, "different dataset_hash"):
                plots.combined_loads(paths)

    def test_svg_exports_are_valid_and_data_survives_rendering(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            rows = [trial("gowave", 10, 1), trial("remnawave", 10, 1, latency=100)]
            startup = [{"target": "gowave", "repetition": 1, "tcp_accept_ms": 20, "api_ready_ms": 30},
                       {"target": "remnawave", "repetition": 1, "tcp_accept_ms": None, "api_ready_ms": None, "failed": True}]
            files = plots.render(path, rows, {"load": {"p99_ms": 500, "max_error_rate": 0.01}}, startup)
            self.assertEqual(files, ["comparison.svg", "reads-rate.svg"])
            for name in files:
                root = ET.parse(path / name).getroot()
                self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
                text = " ".join(root.itertext())
                self.assertIn("gowave", text)
                self.assertIn("remnawave", text)
            data = json.loads((path / "charts.json").read_text())
            self.assertEqual(data["groups"][0]["common_level"], 10)
            self.assertTrue(data["startup"][1]["failed"])
            # Prevent a pre-data axis limit from clipping every measured latency to 0..1.
            curve_text = " ".join(ET.parse(path / "reads-rate.svg").getroot().itertext())
            self.assertIn("500", curve_text)


class ContainerMetadataTests(unittest.TestCase):
    def test_inspect_environment_is_never_written_to_results(self):
        info = {"Id": "container", "Image": "sha256:image", "State": {"Pid": 123},
                "Config": {"Image": "image:version", "Env": ["APP_SECRET=private"],
                           "Labels": {"org.opencontainers.image.revision": "abc"}},
                "HostConfig": {"Memory": 1024, "NanoCpus": 2000000000}}
        metadata = lab.public_metadata(info)
        self.assertNotIn("private", json.dumps(metadata))
        self.assertEqual(metadata["revision"], "abc")
        self.assertEqual(metadata["limits"]["NanoCpus"], 2000000000)


if __name__ == "__main__":
    unittest.main()
