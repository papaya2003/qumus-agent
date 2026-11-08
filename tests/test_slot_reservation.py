"""Slots, chips and stamps a queued job plans on are booked at submit and
re-chosen at run time when taken anyway (BUG A, 2026-10-22: two exfoliations
submitted back-to-back both resolved store_slots mid_chip_1/2 and the second
failed "mid_chip_1 is not empty")."""

from __future__ import annotations

import threading

from conftest import put_sample

from qumus2.testbench.chaos import _world_agrees


def run(s, flow, params, timeout=300):
    return s.jobs.wait(s.jobs.submit(flow, params), timeout)


def events(s, kind):
    return [e for e in s.store.events_after(0, limit=1_000_000) if e.kind == kind]


def test_two_exfoliations_back_to_back_land_in_distinct_slots(services):
    s = services
    a = s.jobs.submit("exfoliate_chips", {"material": "hBN", "chip_ids": ["chip_001", "chip_002"]})
    b = s.jobs.submit("exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_003", "chip_004"]})
    pa, pb = s.store.get_run(a)["params"], s.store.get_run(b)["params"]
    assert pa["store_slots"] == ["mid_chip_1", "mid_chip_2"]
    assert pb["store_slots"] == ["mid_chip_3", "mid_chip_4"]  # booked by a: b plans around it
    booked = s.store.reservations()
    # b also books the Gr carrier and its slide slot: it prepares the mother tape
    assert {k for k, v in booked.items() if v["run_id"] == b} == {
        "mid_chip_3", "mid_chip_4", "chip_003", "chip_004", "carrier_gr_01", "left_slide_1"}
    sa, sb = s.jobs.wait(a, 600), s.jobs.wait(b, 600)
    assert sa.status == "succeeded", [x for x in sa.steps if x["status"] != "done"][:1]
    assert sb.status == "succeeded", [x for x in sb.steps if x["status"] != "done"][:1]
    where = {c: s.store.get_sample(c)["location"]
             for c in ("chip_001", "chip_002", "chip_003", "chip_004")}
    assert len(set(where.values())) == 4 and all(v.startswith("mid_chip") for v in where.values())
    assert s.store.reservations() == {}  # released when the runs ended
    assert _world_agrees(s) == []  # every chip where, and with the material, the ledger says


def test_two_exfoliations_without_chip_ids_take_different_chips(services):
    s = services
    a = s.jobs.submit("exfoliate_chips", {"material": "hBN", "num_chips": 1})
    b = s.jobs.submit("exfoliate_chips", {"material": "Gr", "num_chips": 1})
    ca, cb = s.store.get_run(a)["params"]["chip_ids"], s.store.get_run(b)["params"]["chip_ids"]
    assert ca == ["chip_001"] and cb == ["chip_002"]
    assert s.jobs.wait(a, 600).status == "succeeded"
    assert s.jobs.wait(b, 600).status == "succeeded"
    # on main both runs took chip_001: the second put Gr on the hBN chip and the
    # ledger said Gr, chip_002 stayed bare. The world must agree with the ledger.
    world = {o["id"]: o["material"] for o in s.lab.world.snapshot() if o["kind"] == "chip"}
    ledger = {c["id"]: c["props"].get("material") for c in s.store.list_samples(kind="chip")}
    assert ledger["chip_001"] == world["chip_001"] == "hBN"
    assert ledger["chip_002"] == world["chip_002"] == "Gr"
    assert _world_agrees(s) == []


def test_a_taken_slot_is_re_chosen_at_run_time_and_written_back(services):
    """The booked slot is filled by hand (a job planned before bookings existed,
    or a human) before the store step: the chip goes to the next free slot, the
    run's params say so, and the ledger records the new home."""
    s = services
    rid = s.jobs.submit("exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_001"]})
    assert s.store.get_run(rid)["params"]["store_slots"] == ["mid_chip_1"]
    put_sample(s, "chip_003", "mid_chip_1")  # somebody fills the booked slot meanwhile
    st = s.jobs.wait(rid, 600)
    assert st.status == "succeeded", [x for x in st.steps if x["status"] != "done"][:1]
    assert s.store.get_run(rid)["params"]["store_slots"] == ["mid_chip_2"]
    chip = s.store.get_sample("chip_001")
    assert chip["location"] == "mid_chip_2" and chip["props"]["home_slot"] == "mid_chip_2"
    assert s.lab.world.objects["chip_001"].on is None
    changed = events(s, "job.params_changed")
    assert changed and changed[-1].payload == {"path": ["store_slots", "0"], "value": "mid_chip_2"}
    warn = [e for e in events(s, "notification") if "goes to mid_chip_2 instead" in
            e.payload.get("text", "")]
    assert warn and "mid_chip_1 is taken" in warn[0].payload["text"]


def test_a_searched_chips_home_slot_is_kept_for_it(services):
    """An exfoliation submitted while a chip is away under the microscope must
    not store into that chip's slot (the search job booked it)."""
    s = services
    assert run(s, "exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_001"]}).status == \
        "succeeded"
    assert s.store.get_sample("chip_001")["location"] == "mid_chip_1"
    search = s.jobs.submit("search_chip", {"chip_id": "chip_001"})
    exf = s.jobs.submit("exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_002"]})
    assert s.store.get_run(exf)["params"]["store_slots"] == ["mid_chip_2"]
    assert s.jobs.wait(search, 900).status == "succeeded"
    assert s.jobs.wait(exf, 900).status == "succeeded"
    assert s.store.get_sample("chip_001")["location"] == "mid_chip_1"
    assert s.store.get_sample("chip_002")["location"] == "mid_chip_2"
    assert s.store.reservations() == {}


def test_bookings_lapse_when_a_job_is_cancelled(services):
    s = services
    rid = s.jobs.submit("exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_001"]})
    assert {"mid_chip_1", "chip_001"} <= set(s.store.reservations())  # booked at submit
    s.jobs.stop_all()  # Stop: the job pauses at its next safe point and keeps its bookings
    st = s.jobs.wait(rid, 60)
    assert st.status == "paused", st.status
    assert {"mid_chip_1", "chip_001"} <= set(s.store.reservations())
    s.jobs.cancel(rid)
    assert s.store.get_run(rid)["status"] == "cancelled" and s.store.reservations() == {}
    # the slot and the chip are free for the next job
    nxt = s.jobs.submit("exfoliate_chips", {"material": "Gr", "num_chips": 1})
    assert s.store.get_run(nxt)["params"]["chip_ids"] == ["chip_001"]


def test_submits_from_two_threads_never_share_a_slot(services):
    s, ids = services, []

    def go(mat, chips):
        ids.append(s.jobs.submit("exfoliate_chips", {"material": mat, "chip_ids": chips}))

    ts = [threading.Thread(target=go, args=a) for a in
          (("Gr", ["chip_001"]), ("hBN", ["chip_002"]), ("Gr", ["chip_003"]))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    slots = [s.store.get_run(r)["params"]["store_slots"][0] for r in ids]
    assert len(set(slots)) == 3
