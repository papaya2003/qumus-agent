"""Explicit unaccepted owner-demo launch controls; no provider requests."""
import json
from copy import deepcopy

import pytest
from test_imported_hallbar_target import declared_import

from qumus2.app import build_lab_side
from qumus2.config import simulation_profile
from qumus2.core.optical_profiles import V1_MATERIAL_CAPTURE_PROFILE
from scripts import record_hallbar_demos as recorder


@pytest.fixture
def demo(tmp_path):
    bundle = tmp_path/'bundle'
    bundle.mkdir()
    (bundle/'target.json').write_text(json.dumps(declared_import().model_dump(mode='json')))
    (bundle/'electrode-layout.gds').write_bytes(b'UNIT ONLY; NOT ACTUAL GDS')
    initial, experience = tmp_path/'initial.json', tmp_path/'experience.json'
    initial.write_text('{}')
    experience.write_text('{}')
    config = recorder.simulation_config(target_bundle=bundle)
    identity = recorder.source_identity(initial_state=initial, experience=experience, target_bundle=bundle)
    campaign = recorder.campaign_template(identity, campaign_path=tmp_path/'attempts',
        initial_state=initial, experience=experience, config=config, target_bundle=bundle)
    authorization = tmp_path/'owner.json'
    authorization.write_text(json.dumps({'version':1, 'source_kind':'direct_owner_instruction_relay',
        'scope':recorder.DEMO_SCOPE, 'authorize_paid_first_recording':True,
        'model':'openai/gpt-6-astra', 'cumulative_estimated_usd':100,
        'target_source_sha256':campaign['target_bundle']['binding']['source_sha256'],
        'remaining_fourteen_authorized':False, 'reference':'Declared unit owner relay, not execution permission',
        'instruction_sha256':'a'*64}))
    output = tmp_path/'smoke.log'
    output.write_text('Declared unit receipt, no model requests')
    smoke = tmp_path/'smoke.json'
    smoke.write_text(json.dumps({'version':1,'scope':recorder.DEMO_SCOPE,'identity':identity,
        'checks':dict.fromkeys(recorder.DEMO_CHECKS,True),
        'commands':[{'command':'declared unit smoke','exit_code':0,'output_path':str(output),
                     'output_sha256':recorder._sha_file(output)}]}))
    campaign.update(execution_scope=recorder.DEMO_SCOPE,launch_authorized=True,
        demo_authorization={'path':str(authorization),'sha256':recorder._sha_file(authorization)},
        demo_smoke={'path':str(smoke),'sha256':recorder._sha_file(smoke)})
    return campaign


def test_explicit_demo_release_keeps_scientific_gates_false_and_fourteen_blocked(demo):
    result = recorder.preflight(demo)
    assert result['ready_for_qualification'] and not result['full_experiment_accepted']
    assert demo['integrated_acceptance'] is False and demo['protocol_ready'] is False
    assert not result['other_cells_executable']
    released = recorder.release_first(demo)
    assert released['execution_scope'] == recorder.DEMO_SCOPE
    assert released['scientific_acceptance_claimed'] is False
    assert recorder.TrialRegistry.require_delivery(demo,recorder.FIRST_CELL)['authorized_cells'] == [recorder.FIRST_CELL]


@pytest.mark.parametrize('change',['no_authorization','changed_authorization','wrong_model','wrong_target','other_cells','fake_scientific_pass','missing_smoke','changed_log'])
def test_demo_scope_cannot_bypass_owner_source_or_short_smoke(demo,change):
    campaign = deepcopy(demo)
    if change == 'no_authorization':
        campaign.pop('demo_authorization')
    elif change == 'changed_authorization':
        campaign['demo_authorization']['sha256'] = '0'*64
    elif change in ('wrong_model','wrong_target','other_cells'):
        from pathlib import Path

        p = Path(campaign['demo_authorization']['path'])
        body = json.loads(p.read_text())
        key, value = {'wrong_model':('model','other'), 'wrong_target':('target_source_sha256','0'*64),
                      'other_cells':('remaining_fourteen_authorized',True)}[change]
        body[key] = value
        p.write_text(json.dumps(body))
        campaign['demo_authorization']['sha256'] = recorder._sha_file(p)
    elif change == 'fake_scientific_pass':
        campaign['integrated_acceptance'] = True
    elif change == 'missing_smoke':
        campaign['demo_smoke'] = None
    else:
        from pathlib import Path

        smoke = json.loads(Path(campaign['demo_smoke']['path']).read_text())
        Path(smoke['commands'][0]['output_path']).write_text('changed')
    try:
        result = recorder.preflight(campaign)
    except (ValueError,OSError):
        return
    assert not result['ready_for_qualification']


def test_normal_campaign_still_requires_original_acceptance(demo):
    campaign = deepcopy(demo)
    for key in ('execution_scope','demo_authorization','demo_smoke'):
        campaign.pop(key)
    assert not recorder.preflight(campaign)['ready_for_qualification']


def test_native_rgb_activation_is_explicit_without_changing_default(tmp_path):
    normal = simulation_profile()
    assert 'material_recognition' not in normal
    native = deepcopy(normal)
    native['material_recognition'] = {'backend':'v1_lab_rgb','capture_profile':V1_MATERIAL_CAPTURE_PROFILE}
    side = build_lab_side(tmp_path/'native.db',testbench_config=native,seed=8)
    try:
        assert side.lab.analyzer.recognition_backend == 'v1_lab_rgb'
        assert side.lab.devices.get('microscope').renderer.material_capture_profile == V1_MATERIAL_CAPTURE_PROFILE
    finally:
        assert side.jobs.shutdown()['safe_to_close_resources']
        side.store.close()
    with pytest.raises(ValueError,match='Unknown explicit'):
        build_lab_side(tmp_path/'bad.db',testbench_config={'material_recognition':{'backend':'unknown'}})
    assert not (tmp_path/'bad.db').exists()
