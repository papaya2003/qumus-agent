"""Fresh profile identity and entry-point checks without a server or model."""
from __future__ import annotations

import pytest

from qumus2 import cli
from qumus2.config import load_yaml, simulation_profile
from qumus2.records.device_design import HallBarTarget
from scripts import start_qumus


def test_named_legacy_profiles_preserve_normal_defaults():
    normal = simulation_profile()
    hallbar = simulation_profile("hallbar")
    linked = simulation_profile("hallbar-linked")
    assert ({k: v for k, v in linked.items()
             if k not in ("bench_geometry", "stamp", "initial_samples", "microscope", "material_recognition")}
            == {k: v for k, v in hallbar.items() if k not in ("stamp", "initial_samples", "microscope")})
    # Jialei 2026-11-04 (#182): the linked bench finds flakes the v1 way, from pixels.
    assert linked["material_recognition"]["backend"] == "v1_lab_rgb"
    assert "material_recognition" not in hallbar
    # The stock follows the bench: six slots on the older bench, nine on the linked board.
    stock = lambda config: [s for s in config["initial_samples"] if s.get("props", {}).get("stock_source")]  # noqa: E731
    assert ([s for s in linked["initial_samples"] if s not in stock(linked)]
            == [s for s in hallbar["initial_samples"] if s not in stock(hallbar)])
    linked_raw = [s["location"] for s in linked["initial_samples"] if s["kind"] == "chip" and s["location"].startswith("left_chip_")]
    assert linked_raw == [f"left_chip_{i}" for i in range(1, 10)]
    # #204: inferred flat dome (from the v1 presets, not measured); no v2 stamp discovery.
    inferred = {"dome_radius_mm": 5.7, "dome_height_mm": 0.05, "indentation_per_travel": 0.25,
                "crush_depth_mm": 0.6, "touch_z": 5.2, "touch_z_sigma": 0.02}
    assert {k: linked["stamp"][k] for k in inferred} == inferred
    assert "cap_support_radius_mm" not in linked["stamp"] and "discovery" not in linked["stamp"]
    for key in set(hallbar["stamp"]) - {*inferred, "viscoelastic_tau_s", "dynamics", "pickup_first"}:
        assert linked["stamp"][key] == hallbar["stamp"][key]
    # Jialei 2026-11-03: the bare PC film generally does not lift graphene.
    assert linked["stamp"]["pickup_first"] == {**hallbar["stamp"]["pickup_first"], "Gr": 0.02}
    assert linked["stamp"]["pickup_stack_requires_overlap"] is True
    assert "pickup_stack_requires_overlap" not in hallbar["stamp"]
    # Jialei 2026-11-03: zero stamp drift and contact twist in the linked Hall-bar SIM only.
    assert linked["stamp"]["dynamics"] == {**hallbar["stamp"]["dynamics"], "lateral_drift_mm_s": [1e-8, -1e-8],
                                           "approach_drift_mm_per_mm": [0.0, 0.0], "contact_slip_deg_s": 0}
    assert hallbar["stamp"]["dynamics"]["contact_slip_deg_s"] == 0.002
    # Jialei 2026-11-04: the linked SIM keeps the chip-corner detection error (#167 corrects it).
    # corner error kept: electrodes register the frame (#167); Jialei 2026-11-04: no drawn tape residue
    # Jialei 2026-11-05: no chip tilt in the linked Hall-bar SIM.
    assert linked["microscope"] == {**hallbar["microscope"], "residue": False, "best_focus_sigma": 0.02,
                                    "focus_tilt_per_mm": 0.0}
    assert hallbar["microscope"].get("residue", True) is True
    assert hallbar["microscope"]["corner_sigma_mm"] == 0.003
    assert hallbar["seed"] == 8
    raw = [s for s in hallbar["initial_samples"] if s["kind"] == "chip" and s["location"].startswith("left_chip_")]
    assert [s["location"] for s in raw] == [f"left_chip_{i}" for i in range(1, 7)]
    assert all(s["status"] == "bare" for s in raw)
    assert not any(s["kind"] in ("flake", "device") for s in hallbar["initial_samples"])
    target = next(s for s in hallbar["initial_samples"] if s["id"] == "chip_e01")
    typed = HallBarTarget.model_validate(target["props"]["device_design_target"])
    assert len(typed.contacts) == 8 and target["props"]["layers_top_first"] == []
    assert hallbar["placement"] == normal["placement"]
    assert hallbar["stamp"] == normal["stamp"]
    assert hallbar["reactor"] == {"enabled": True, "synthetic_prepared_pairs": False}
    assert simulation_profile() == normal
    assert len([s for s in normal["initial_samples"] if s["location"].startswith("left_chip_")]) == 4
    assert normal["seed"] == load_yaml("testbench.yaml")["seed"]
    hallbar["initial_samples"][0]["status"] = "changed only in caller"
    assert simulation_profile() == normal and simulation_profile("hallbar-linked") == linked


def test_cli_always_serves_the_linked_hallbar_bench_before_any_service(tmp_path, monkeypatch):
    """One bench only (owner, 2026-11-10): no option selects an older layout."""
    from qumus2 import app
    from qumus2.config import SIM_PROFILE

    seen = []
    class BeforeServices(Exception):
        pass
    def capture(database, **kwargs):
        seen.append((database, kwargs))
        raise BeforeServices
    monkeypatch.setattr(app, "build_services", capture)
    database = tmp_path / "fresh" / "qumus.db"
    with pytest.raises(BeforeServices):
        cli.main(["serve", "--db", str(database), "--mock", "--lab-process", "--no-browser"])
    assert seen[0][0] == str(database)
    effective = seen[0][1]
    assert effective["use_openai"] is False and effective["lab_process"] is True
    assert SIM_PROFILE == "hallbar-linked"
    assert effective["testbench_config"] == simulation_profile("hallbar-linked")
    assert effective["scenario"] == "hallbar_device"
    assert not database.parent.exists()


@pytest.mark.parametrize("options", [["--profile", "previous"], ["--profile", "integrated"],
                                     ["--profile", "hallbar"], ["--reactor"]])
def test_cli_has_no_older_bench_options(tmp_path, options):
    with pytest.raises(SystemExit):
        cli.main(["serve", "--db", str(tmp_path / "qumus.db"), "--mock", "--no-browser", *options])


@pytest.mark.parametrize("option", ["--linked-workspace", "--hallbar-workspace", "--integrated-workspace",
                                    "--previous-workspace", "--reactor"])
def test_launcher_has_one_entry_and_no_old_workspace_switches(monkeypatch, option):
    """Jialei 2026-11-04: one stable entry; the old workspaces stay on disk
    but have no launcher switch."""
    monkeypatch.setattr(start_qumus.sys, "argv", ["start_qumus.py", "--check-only", option])
    monkeypatch.setattr(start_qumus.os, "execv", lambda *a: pytest.fail("started a server"))
    with pytest.raises(SystemExit) as result:
        start_qumus.main()
    assert result.value.code == 2
