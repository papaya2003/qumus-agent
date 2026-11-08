"""A testbench serve on a run store kept from an earlier serve (runs/qumus.db).

The new bench is seeded fresh, so the ledger must match it: what an earlier
bench made (flakes, a device on the target chip) leaves the ledger with a
`sample.removed` event, and the seeded samples start without their old props.
Otherwise the Lab page showed "No free target chip" on a new bench and the
agents saw flakes that the new bench does not have."""

from fastapi.testclient import TestClient

from qumus2.app import build_services
from qumus2.ui.app import create_app


def test_a_new_bench_clears_what_an_earlier_bench_made(tmp_path):
    db = tmp_path / "run" / "qumus.db"
    first = build_services(db, scenario="place_chip_demo", seed=2)
    seeded = {x["id"]: x for x in first.store.list_samples()}
    target = next(x for x in seeded.values() if x["kind"] == "chip" and x["location"].startswith("right"))
    first.store.upsert_sample("device_001", "device", location=target["id"], status="stacked")
    first.store.upsert_sample("flake_001", "flake", location=target["id"], status="catalogued",
                              props={"material": "graphene"})
    first.store.upsert_sample(target["id"], "chip", status="device", props={"stack": ["flake_001"]})
    first.store.close()

    second = build_services(db, scenario="place_chip_demo", seed=2)
    now = {x["id"]: x for x in second.store.list_samples()}
    assert set(now) == set(seeded)
    assert now[target["id"]]["status"] == target["status"] and "stack" not in now[target["id"]]["props"]
    removed = [e for e in second.store.events_after(second.session_start, limit=10_000)
               if e.kind == "sample.removed"]
    assert sorted(e.payload["id"] for e in removed) == ["device_001", "flake_001"]
    with TestClient(create_app(second, start_driver=False)) as c:
        ids = {x["id"] for x in c.get("/api/snapshot").json()["samples"]}
    assert "device_001" not in ids and "flake_001" not in ids
