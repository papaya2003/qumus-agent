"""The brain commands stations through contract 2 (lab-contract's labclient) when they offer it.

FakeLab(contract2=True) serves its commands as one contract 2 station (labkit) next to its v1
ports; the record feed, pictures and put_records stay on v1, as with qumus-simlab. Needs a
lab-contract checkout (beside this repository or in QUMUS_LAB_CONTRACT); skipped without one.
"""

from __future__ import annotations

import pytest
from conftest import _close, make_fake_lab

from qumus2.app import build_services
from qumus2.contract import ContractClient, ContractVersionError
from qumus2.stations.link import V1Link, V2Link, load_labclient, open_link

pytestmark = pytest.mark.skipif(load_labclient() is None, reason="lab-contract checkout not found")


@pytest.fixture
def lab2():
    lab = make_fake_lab(contract2=True)
    yield lab
    lab.close()


@pytest.fixture
def services2(lab2, tmp_path):
    s = build_services(tmp_path / "qumus.db", stations=lab2.stations(), mirror_poll_s=0.05)
    s.fake_lab = lab2
    yield s
    _close(s)


def test_jobs_go_through_contract2_and_come_back_through_the_feed(services2):
    s, lab = services2, services2.fake_lab
    assert s.jobs.contracts() == {"exfoliation": 2, "stacking": 2, "reactor": 2}
    assert "search_chip" in s.jobs.flow_names() and "put_records" not in s.jobs.flow_names()
    lab.outcomes["search_chip"] = lambda run, p: {
        "status": "succeeded", "result": {"flakes": []},
        "steps": [{"name": "chip_to_microscope"}, {"name": "scan"}]}
    s.store.upsert_sample("chip_002", "chip", props={"brain_note": "picked"})
    rid = s.jobs.submit("search_chip", {"chip_id": "chip_002", "material": "hBN"},
                        project_id=s.project_id, requested_by="hands")
    st = s.jobs.wait(rid, 5)
    assert st.done and st.status == "succeeded" and [x["name"] for x in st.steps] == [
        "chip_to_microscope", "scan"]
    assert lab.commands[-1][:2] == ("stacking", "search_chip")
    assert lab.store.get_sample("chip_002")["props"]["brain_note"] == "picked"  # put_records (v1)
    # the command went to the contract 2 station with a client command id and the lease
    svc = lab.station2.services["stacking"]
    rec = next(r for r in svc.records.values() if r["action"] == "search_chip")
    assert rec["result"] == "ok" and rec["client"] == "qumus-agent" and rec["data"]["run_id"] == rid
    assert svc.holder == "qumus-agent"
    # job controls, unknown flows and stop
    s.jobs.cancel(rid)
    assert ("control", "job_cancel", {"run_id": rid}) in lab.commands
    s.jobs.retry(rid)
    assert s.jobs.wait(rid, 5).status == "succeeded"
    assert s.jobs.complete_experiment(s.project_id, "done")["completed"] is True
    with pytest.raises(KeyError):
        s.jobs.submit("bake_a_cake", {})
    s.jobs.stop_all()
    assert any(r["action"] == "search_chip" for r in svc.records.values())
    s.jobs.close()
    assert svc.holder is None  # the lease was given back


def test_a_refused_lease_and_failed_job_read_as_before(services2):
    s, lab = services2, services2.fake_lab
    stacking = lab.station2.services["stacking"]
    stacking.lease_post("someone-else", False)
    with pytest.raises(ValueError, match="controlled by someone else"):
        s.jobs.submit("search_chip", {"chip_id": "chip_002"}, project_id=s.project_id)
    stacking.lease_delete("someone-else")

    def boom(run, params):
        raise ValueError("no such chip")

    lab.services["stacking"].commands["search_chip"].run = boom
    with pytest.raises(ValueError, match="search_chip on stacking"):
        s.jobs.submit("search_chip", {"chip_id": "chip_404"}, project_id=s.project_id)


def test_the_path_is_chosen_per_module(lab2):
    client = ContractClient(lab2.urls["stacking"], "qumus-agent")
    assert isinstance(open_link("stacking", client, {"entry": lab2.entry}), V2Link)
    assert isinstance(open_link("stacking", client, {"entry": lab2.entry, "contract": "1"}), V1Link)
    # the v1-only port does not offer contract 2: auto falls back, `contract: 2` refuses
    assert isinstance(open_link("stacking", client), V1Link)
    with pytest.raises(ContractVersionError, match="contract 2 required"):
        open_link("stacking", client, {"contract": "2"})
    with pytest.raises(ContractVersionError, match="no service"):
        open_link("stacking", client, {"entry": lab2.entry, "contract": "2", "service": "nope"})
    with pytest.raises(ValueError):
        open_link("stacking", client, {"contract": "3"})
