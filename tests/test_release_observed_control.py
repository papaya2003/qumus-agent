"""Mock-device controller/receipt controls, without a World or laboratory job."""
from copy import deepcopy

import pytest

from qumus2.core.errors import NeedsHumanError
from qumus2.flows.library import stacking as m
from tests.test_v1_transfer_sequence import _real_release_heater
from tests.test_v1_transfer_sequence import motion_case as original_motion_case


@pytest.fixture
def motion_case(tmp_path):
    return original_motion_case.__wrapped__(tmp_path)


@pytest.mark.parametrize('supported', [True, False])
def test_existing_segmented_controller_stops_on_first_support_or_original_floor(motion_case, monkeypatch, supported):
    case = motion_case
    step, heater, trace = _real_release_heater(case, monkeypatch, 'PC', 155.0, 170.0)
    case.ctx.params['release_policy'] = m.OBSERVED_RELEASE_POLICY_ID
    reference = {'policy': m.OBSERVED_RELEASE_POLICY_ID, 'requested_z_mm': 5.605}
    process = {'minimum_z_mm': 5.6, 'reference_interval_mm': [5.7, 5.8]}
    monkeypatch.setattr(m, '_release_reference', lambda *a: (deepcopy(process), deepcopy(reference)))
    acquired = []
    monkeypatch.setattr(m, '_acquire_release_footprint', lambda ctx, phase: acquired.append(phase['observations']))
    monkeypatch.setattr(m, '_freeze_release_approach', lambda ctx, phase: None)
    observed = []

    def support(ctx, phase, frame):
        observed.append(m._pos(ctx)['z'])
        return supported and m._pos(ctx)['z'] <= 6.45
    monkeypatch.setattr(m, '_observe_release_support', support)
    if supported:
        step.act(case.ctx, None)
        phase = case.metadata['normal_transfer']['phases'][step.name]
        assert phase['complete'] and phase['outputs_off']
        assert min(v[2] for v in case.moves) >= 6.43
        assert observed[-1] > reference['requested_z_mm']
    else:
        with pytest.raises(NeedsHumanError, match='original release floor'):
            step.act(case.ctx, None)
        assert observed[-1] == pytest.approx(reference['requested_z_mm'])
        assert not any((p['params']['celsius'] > 155.5 for kind, p in trace if kind == 'device.command' and p.get('command') == 'set_target'))
    assert acquired == [1]
    assert heater.get_target() is None
    assert all(v[2] >= process['minimum_z_mm'] for v in case.moves)
    assert len(observed) > 1 and all((b < a for a, b in zip(observed, observed[1:], strict=False)))

def test_deep_envelope_and_footprint_focus_have_real_mandatory_reserve(motion_case, monkeypatch):
    case = motion_case
    _, _, _ = _real_release_heater(case, monkeypatch, 'PC', 155.0, 170.0)
    phase = m._begin_phase(case.ctx, 'release')
    phase['release_reference'] = {'requested_z_mm': 5.7}
    phase['release_budget'] = {'stage': 'entry_heat', 'park_observations': 0, 'endpoint_limit': 24, 'endpoint_observations': 0}
    shallow = m._release_work(case.ctx, phase)
    phase['release_reference']['requested_z_mm'] = 5.605
    deep = m._release_work(case.ctx, phase)
    assert deep['observations'] > shallow['observations']
    assert deep['z_travel_mm'] > shallow['z_travel_mm']
    phase['release_budget'].update(footprint_pending=22, footprint_focus_bound_s=0.5)
    focus = m._release_work(case.ctx, phase)
    assert focus['observations'] == deep['observations'] + 22
    assert focus['seconds'] == deep['seconds'] + 11
    phase['observations'] = 600 - focus['observations'] + 1
    with pytest.raises(NeedsHumanError, match='mandatory'):
        m._release_budget_guard(case.ctx, phase)
