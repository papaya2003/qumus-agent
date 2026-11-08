"""The Hall-bar raw stock matches the real lab's nine blank source chips."""
import pytest

from qumus2.app import build_services
from qumus2.config import simulation_profile
from qumus2.testbench.hallbar import with_hallbar_target


def bare(config):
    return {s["id"]: s["location"] for s in config["initial_samples"]
            if s["kind"] == "chip" and s.get("status") == "bare" and s["id"].startswith("chip_0")}


def linked_bench():
    config = simulation_profile("integrated")
    config["bench_geometry"] = simulation_profile("hallbar-linked")["bench_geometry"]
    return config


def test_linked_bench_default_stock_is_nine_bare_chips_in_left_slots():
    chips = bare(with_hallbar_target(linked_bench()))
    assert chips == {f"chip_{i:03d}": f"left_chip_{i}" for i in range(1, 10)}
    assert bare(simulation_profile("hallbar-linked")) == chips


def test_older_bench_default_keeps_its_six_slots():
    assert bare(with_hallbar_target(simulation_profile("integrated"))) == {
        f"chip_{i:03d}": f"left_chip_{i}" for i in range(1, 7)}


@pytest.mark.parametrize("profile", ["hallbar", "hallbar-linked"])
def test_both_hallbar_benches_build(profile):
    services = build_services(testbench_config=simulation_profile(profile), use_openai=False)
    assert {s["id"] for s in services.store.list_samples(kind="chip") if s["status"] == "bare"} >= set(
        bare(simulation_profile(profile)))


@pytest.mark.parametrize("count", [4, 6])
def test_older_fixture_stocks_stay_available(count):
    assert len(bare(with_hallbar_target(simulation_profile("integrated"), raw_stock_count=count))) == count


@pytest.mark.parametrize("count", [5, 7, 12, True])
def test_undeclared_stock_counts_are_refused(count):
    with pytest.raises(ValueError):
        with_hallbar_target(simulation_profile("integrated"), raw_stock_count=count)
