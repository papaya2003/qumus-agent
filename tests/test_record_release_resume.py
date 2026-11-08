"""A saved failure must not become a clean recording through the resume path."""
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

spec = spec_from_file_location('record_release', Path(__file__).parents[1] / 'scripts/record_release.py')
release = module_from_spec(spec)
spec.loader.exec_module(release)


def evidence():
    return ({'case': 'reactor', 'passed': False, 'error': None,
             'checks': {'model_turns_completed': False, 'physical_plan_closed': False,
                        'cold_storage_verified': True, 'no_unfinished_job': True}},
            {'seed': 7, 'simulation_profile': 'integrated', 'material_profile': 'default',
             'execution_source_sha256': 'recorded-source'},
            [{'kind': 'agent.turn_end', 'payload': {'is_error': True, 'reason': 'budget_exhausted'}}])


def test_budget_only_resume_retains_the_original_failure_evidence():
    values = evidence()
    before = deepcopy(values)
    release.validate_budget_resume(*values, case='reactor', seed=7, morphology=False)
    assert values == before


@pytest.mark.parametrize('failure', ['incomplete_physics', 'parked_job', 'model_error',
                                    'driver_error', 'already_passed', 'no_budget_stop',
                                    'different_seed', 'different_profile', 'missing_source'])
def test_resume_refuses_other_failures_or_changed_setup(failure):
    previous, meta, events = evidence()
    if failure == 'incomplete_physics':
        previous['checks']['cold_storage_verified'] = False
    elif failure == 'parked_job':
        previous['checks']['no_unfinished_job'] = False
    elif failure == 'model_error':
        events[0]['payload']['reason'] = 'model_error'
    elif failure == 'driver_error':
        previous['error'] = 'driver timed out'
    elif failure == 'already_passed':
        previous['passed'] = True
    elif failure == 'no_budget_stop':
        events = []
    elif failure == 'different_seed':
        meta['seed'] = 2
    elif failure == 'different_profile':
        meta['material_profile'] = 'material-morphology.yaml'
    else:
        del meta['execution_source_sha256']
    with pytest.raises(ValueError, match='budget'):
        release.validate_budget_resume(previous, meta, events, case='reactor', seed=7, morphology=False)


def repair_evidence():
    return ({'case': 'collaboration-recovery', 'passed': False,
             'error': 'Material preparation did not finish for chip_002 (Gr); stop this acceptance attempt.',
             'jobs': [{'flow': 'inspect_bench', 'status': 'succeeded'}],
             'operator_inputs': [{'kind': 'message'}, {'kind': 'plan_change', 'sent_while_busy': True}]},
            {'seed': 7, 'simulation_profile': 'integrated', 'material_profile': 'default',
             'execution_source_sha256': 'actual-original-source'},
            [{'kind': 'tool.result', 'payload': {'tool': 'update_plan', 'status': 'error',
               'summary': 'Physical cancellation/replacement requires a new, explicit user instruction.'}},
             {'kind': 'agent.turn_end', 'payload': {'is_error': False}}])


def test_repair_continuation_requires_an_unstarted_recorded_plan_refusal():
    values = repair_evidence()
    before = deepcopy(values)
    release.validate_preparation_repair_resume(*values, case='collaboration-recovery', seed=7, morphology=False)
    assert values == before


@pytest.mark.parametrize('failure', ['physical_work', 'parked_inspection', 'already_passed',
                                    'wrong_failure', 'no_refusal', 'model_error', 'faults_armed',
                                    'different_seed', 'different_profile', 'missing_source'])
def test_repair_continuation_never_discards_other_failure_evidence(failure):
    previous, meta, events = repair_evidence()
    if failure == 'physical_work':
        previous['jobs'].append({'flow': 'exfoliate_chips', 'status': 'succeeded'})
    elif failure == 'parked_inspection':
        previous['jobs'][0]['status'] = 'parked'
    elif failure == 'already_passed':
        previous['passed'] = True
    elif failure == 'wrong_failure':
        previous['error'] = 'Driver timed out'
    elif failure == 'no_refusal':
        events.pop(0)
    elif failure == 'model_error':
        events.append({'kind': 'agent.error', 'payload': {}})
    elif failure == 'faults_armed':
        events.append({'kind': 'fault.injected', 'payload': {}})
    elif failure == 'different_seed':
        meta['seed'] = 2
    elif failure == 'different_profile':
        meta['simulation_profile'] = 'previous'
    else:
        del meta['execution_source_sha256']
    with pytest.raises(ValueError, match='Repair continuation'):
        release.validate_preparation_repair_resume(previous, meta, events,
            case='collaboration-recovery', seed=7, morphology=False)
