"""Hall-bar lab flake supply (Jialei, 2026-11-03): more and larger flakes in the
hallbar profiles only; the simulation models the process, not the material."""
import pytest

from qumus2.app import build_services
from qumus2.config import HALLBAR_SUPPLY, simulation_profile
from tests.test_stacking import run


@pytest.mark.parametrize("profile", ["hallbar", "hallbar-linked"])
def test_hallbar_profiles_raise_yield_and_size(profile):
    exf = simulation_profile(profile)["exfoliation"]
    assert exf["flakes_per_chip"] == HALLBAR_SUPPLY["flakes_per_chip"] == 30
    assert exf["morphology"]["median_area_um2"] == HALLBAR_SUPPLY["median_area_um2"] == 600


def test_other_profiles_keep_the_default_supply():
    exf = simulation_profile()["exfoliation"]
    assert exf["flakes_per_chip"] == 20 and "median_area_um2" not in exf.get("morphology", {})


def test_the_hallbar_lab_deposits_more_and_larger_flakes():
    def flakes(config):
        s = build_services(":memory:", testbench_config=config, use_openai=False)
        st = run(s, "exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_001"]}, timeout=900)
        assert st.status == "succeeded"
        return s.lab.world.objects["chip_001"].flakes

    hallbar = simulation_profile("hallbar-linked")
    plain = simulation_profile("hallbar-linked")
    plain["exfoliation"]["flakes_per_chip"] = 20
    plain["exfoliation"]["morphology"].pop("median_area_um2")
    rich, poor = flakes(hallbar), flakes(plain)
    assert len(rich) > len(poor)
    median = lambda fl: sorted(f.area_um2 for f in fl)[len(fl) // 2]  # noqa: E731
    assert median(rich) > median(poor)
