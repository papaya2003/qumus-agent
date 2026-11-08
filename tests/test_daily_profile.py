"""The everyday entry requires its owner bundle and declares its fresh fixture."""

import pytest

from qumus2.config import load_yaml, simulation_profile
from qumus2.testbench.reactor_config import with_reactor


def test_default_launcher_requires_owner_bundle_and_preserves_previous_routes(tmp_path, monkeypatch, capsys):
    from scripts import start_qumus

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(start_qumus, "REPO", repo)
    monkeypatch.setattr(start_qumus.sys, "argv", ["start_qumus.py", "--check-only"])
    with pytest.raises(SystemExit) as result:
        start_qumus.main()
    assert result.value.code == 2 and "Startup blocked" in capsys.readouterr().out
    assert not (repo / "runs").exists()


def test_daily_profile_has_reactor_without_prepared_pair_shortcut():
    config = simulation_profile()
    assert config["placement"] == load_yaml("testbench.yaml")["placement"]
    bench, composed = with_reactor(load_yaml("bench.yaml"), config)
    assert "arm_reactor" in bench
    assert not any(s.get("props", {}).get("reactor_pair") for s in composed["initial_samples"])
    assert not simulation_profile("previous")["reactor"]["enabled"]
