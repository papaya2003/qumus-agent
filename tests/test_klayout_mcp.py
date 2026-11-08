"""Offline MCP envelope/immutable-binding controls; no live KLayout acceptance."""
from __future__ import annotations

import ast
import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from test_device_design_pose_screen import declared_spec

from qumus2.agents import klayout
from qumus2.records.device_design import (
    DesignArtifact,
    DeviceDesignV1,
    KLayoutMCPArtifact,
    bind_artifact,
    design_directory,
    validate_design_artifact,
)
from qumus2.records.geometry import geometry_digest

ATOMIC_SCHEMA = {"type": "object", "properties": {
    "name": {"type": "string"}, "dbu": {"type": "number"}},
    "required": ["name", "dbu"], "additionalProperties": False,
    "description": "atomic-new-layout-v1"}


def _script_data(code):
    values = {}
    for node in ast.parse(code).body:
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "payload":
            values['payload'] = json.loads(ast.literal_eval(node.value.args[0]))
        elif isinstance(target, ast.Name) and target.id == "root":
            values['root'] = Path(ast.literal_eval(node.value.args[0]))
        elif isinstance(target, ast.Name) and target.id == "zoom":
            values['zoom'] = ast.literal_eval(node.value)
        elif isinstance(target, ast.Tuple) and [t.id for t in target.elts] == ['nonce', 'marker']:
            values['nonce'], values['marker'] = ast.literal_eval(node.value)
    return values


class _Response:
    def __init__(self, value, status=200):
        self.status, self.fp = status, None
        self.body = io.BytesIO(json.dumps(value).encode())

    def getheader(self, name, default=None):
        return default

    def read1(self, count):
        return self.body.read(min(count, 4096))


class MockBridge:
    """Fake transport publications, explicitly not GDS/pya execution."""
    def __init__(self, monkeypatch, *, change=None):
        self.requests = []
        self.change = change
        owner = self

        class Connection:
            sock = None

            def __init__(self, host, port, remaining):
                assert host in {'localhost', '127.0.0.1', '::1'} and port == 8767
                assert remaining() > 0

            def request(self, method, path, raw, headers):
                assert method == 'POST' and path == '/mcp'
                self.request = json.loads(raw)
                owner.requests.append(self.request)

            def getresponse(self):
                if owner.change == 'timeout':
                    raise TimeoutError('offline control')
                result = owner.answer(self.request)
                return _Response({'jsonrpc': '2.0', 'id': self.request['id'], 'result': result},
                                 status=302 if owner.change == 'redirect' else 200)

            def close(self):
                pass

        monkeypatch.setattr(klayout, '_DeadlineConnection', Connection)

    def answer(self, request):
        if request['method'] == 'initialize':
            return {'protocolVersion': klayout.PROTOCOL,
                    'serverInfo': {'name': 'KlayoutClaw', 'version': '0.6'}}
        if request['method'] == 'tools/list':
            properties = {'code': {'type': 'string'}}
            if self.change != 'unpatched':
                properties['atomic_new_layout'] = copy.deepcopy(ATOMIC_SCHEMA)
            return {'tools': [{'name': 'execute_script', 'inputSchema': {'properties': properties}}]}
        assert request['method'] == 'tools/call'
        params = request['params']
        assert params['name'] == 'execute_script'
        if self.change == 'tool_error':
            return {'isError': True, 'content': [{'type': 'text', 'text': 'Server busy'}]}
        if self.change == 'scalar_content':
            return {'content': [3]}
        data = _script_data(params['arguments']['code'])
        assert params['arguments']['atomic_new_layout'] == {'name': data['marker'], 'dbu': .001}
        root = data['root']
        (root / 'device.gds').write_bytes(b'EXPLICIT OFFLINE TRANSPORT DOUBLE; NOT ACTUAL GDS')
        pixels = Image.new('RGB', (1280, 960), '#151515')
        pixels.putpixel((640, 480), (255, 100, 0))
        pixels.save(root / 'native.png')
        value = {'status': 'verified', 'payload': data['payload'], 'nonce': data['nonce'],
                 'gds_sha256': hashlib.sha256((root / 'device.gds').read_bytes()).hexdigest(),
                 'image_sha256': hashlib.sha256((root / 'native.png').read_bytes()).hexdigest(),
                 'view_identity': data['nonce']+':owned-double', 'view_index': 1,
                 'layer_names': [i['name'] for i in data['payload']['layers']],
                 'zoom_box_um': data['zoom'], 'stdout': '', 'stderr': '',
                 'atomic_new_layout': {'profile': klayout.ATOMIC_PROFILE, 'name': data['marker'], 'dbu': .001}}
        if self.change == 'wrong_owner':
            value['view_identity'] = 'user-tab'
        if self.change == 'wrong_atomic':
            value['atomic_new_layout']['name'] = 'user-tab'
        if self.change == 'protections':
            value['protections_applied'] = [{'action': 'replaced invalid view'}]
        if self.change == 'wrong_payload':
            value['payload']['layers'][0]['points_dbu'][0][0] += 1
        if self.change == 'missing_image':
            (root / 'native.png').unlink()
        return {'content': [{'type': 'text', 'text': json.dumps(value)}]}


def fixture(tmp_path):
    return SimpleNamespace(path=str(tmp_path / 'qumus.db'), bench='sim'), declared_spec()


def test_mock_wire_chain_binds_native_receipt_and_preserves_typed_artifact(tmp_path, monkeypatch):
    bridge = MockBridge(monkeypatch)
    store, spec = fixture(tmp_path)
    artifact = klayout.create_artifact(store, spec)
    assert type(artifact) is KLayoutMCPArtifact
    design = bind_artifact(spec, artifact)
    assert type(DeviceDesignV1.model_validate(design.model_dump(mode='json')).artifact) is KLayoutMCPArtifact
    assert validate_design_artifact(store, design) == design
    assert [r['method'] for r in bridge.requests] == ['initialize', 'tools/list', 'tools/call']
    assert bridge.requests[-1]['params']['name'] == 'execute_script'
    assert all(r.get('params', {}).get('name') not in {'save_layout', 'screenshot', 'close_layout_view'}
               for r in bridge.requests)
    data = _script_data(bridge.requests[-1]['params']['arguments']['code'])
    # Complete-source envelope, in um; not the 1000um unit fixture chip.
    assert data['zoom'][2]-data['zoom'][0] < 200
    assert len(data['payload']['layers']) == 10
    assert (design_directory(store, spec.design_id) / 'mcp-transport.json').is_file()


@pytest.mark.parametrize('change', ['unpatched', 'timeout', 'redirect', 'tool_error', 'scalar_content',
    'wrong_owner', 'wrong_atomic', 'protections', 'wrong_payload', 'missing_image'])
def test_mock_mcp_failure_remains_unaccepted_without_batch_or_cleanup(tmp_path, monkeypatch, change):
    bridge = MockBridge(monkeypatch, change=change)
    store, spec = fixture(tmp_path)
    with pytest.raises(klayout.KLayoutError):
        klayout.create_artifact(store, spec)
    assert (design_directory(store, spec.design_id) / 'roundtrip.py').is_file()
    if change in {'unpatched', 'timeout', 'redirect'}:
        assert not any(r['method'] == 'tools/call' for r in bridge.requests)
    assert not any(r.get('params', {}).get('name') == 'close_layout_view' for r in bridge.requests)


@pytest.mark.parametrize('url', ['https://localhost:8767/mcp', 'http://example.org:8767/mcp',
    'http://localhost:8765/mcp', 'http://user@localhost:8767/mcp', 'http://localhost:8767/mcp?script=x'])
def test_endpoint_cannot_redirect_trusted_code_or_paths(monkeypatch, url):
    monkeypatch.setenv('QUMUS_KLAYOUT_MCP_URL', url)
    with pytest.raises(klayout.KLayoutError):
        klayout.endpoint()


@pytest.mark.parametrize('file', ['native.png', 'verified.json', 'roundtrip.py', 'device.gds'])
def test_saved_mcp_receipt_or_pixels_tamper_refuses(tmp_path, monkeypatch, file):
    MockBridge(monkeypatch)
    store, spec = fixture(tmp_path)
    design = bind_artifact(spec, klayout.create_artifact(store, spec))
    path = design_directory(store, spec.design_id) / file
    path.write_bytes(path.read_bytes()+b'tampered')
    with pytest.raises(ValueError, match='changed|differs'):
        validate_design_artifact(store, design)


def test_historical_batch_dump_and_digest_remain_exact_without_new_fields():
    spec = declared_spec()
    artifact_body = {'path': '/historical/device.gds', 'sha256': '4'*64,
                     'backend': 'klayout_batch', 'dbu_um': .001, 'geometry_sha256': '5'*64}
    artifact = DesignArtifact.model_validate(artifact_body)
    assert artifact.model_dump(mode='json') == artifact_body
    body = {**spec.model_dump(mode='json'), 'artifact': artifact_body}
    original = {**body, 'canonical_digest': geometry_digest(body)}
    assert DeviceDesignV1.model_validate(original).model_dump(mode='json') == original
    assert 'receipt_sha256' not in original['artifact']


@pytest.mark.parametrize('change', ['missing_receipt', 'invalid_hash', 'legacy_backend', 'unknown_backend'])
def test_union_cannot_downgrade_mcp_or_drop_receipt(tmp_path, monkeypatch, change):
    MockBridge(monkeypatch)
    store, spec = fixture(tmp_path)
    body = bind_artifact(spec, klayout.create_artifact(store, spec)).model_dump(mode='json')
    if change == 'missing_receipt':
        del body['artifact']['receipt_sha256']
    elif change == 'invalid_hash':
        body['artifact']['receipt_sha256'] = 'invalid'
    else:
        body['artifact']['backend'] = 'klayout_batch' if change == 'legacy_backend' else 'pretend_mcp'
    body['canonical_digest'] = geometry_digest({k: v for k, v in body.items() if k != 'canonical_digest'})
    with pytest.raises(ValueError):
        DeviceDesignV1.model_validate(body)


def test_every_underlying_send_receive_uses_remaining_total_deadline(monkeypatch):
    import socket as socket_module

    calls = []
    monkeypatch.setattr(socket_module.socket, 'recv_into', lambda self, *a, **k: calls.append(('recv', self.gettimeout())) or 1)
    monkeypatch.setattr(socket_module.socket, 'sendall', lambda self, *a, **k: calls.append(('send', self.gettimeout())))
    limits = iter([.2, .1])
    sock = klayout._DeadlineSocket(socket_module.AF_INET, lambda: next(limits))
    try:
        sock.recv_into(bytearray(1))
        sock.sendall(b'x')
        assert calls == [('recv', .2), ('send', .1)]
        def expired():
            raise klayout.KLayoutError('expired')
        sock._remaining = expired
        with pytest.raises(klayout.KLayoutError, match='expired'):
            sock.recv_into(bytearray(1))
        assert len(calls) == 2
    finally:
        sock.close()


@pytest.mark.parametrize('bad', ['server', 'schema', 'atomic', 'name', 'required', 'description'])
def test_malformed_nested_schema_refuses_before_any_layout(tmp_path, monkeypatch, bad):
    bridge = MockBridge(monkeypatch)
    answer = bridge.answer
    def changed(request):
        result = answer(request)
        if request['method'] == 'initialize' and bad == 'server':
            result['serverInfo'] = 3
        if request['method'] == 'tools/list':
            schema = result['tools'][0]['inputSchema']
            if bad == 'schema':
                result['tools'][0]['inputSchema'] = 3
            elif bad == 'atomic':
                schema['properties']['atomic_new_layout'] = []
            elif bad == 'name':
                schema['properties']['atomic_new_layout']['properties']['name'] = None
            elif bad == 'required':
                schema['properties']['atomic_new_layout']['required'] = [{}]
            elif bad == 'description':
                schema['properties']['atomic_new_layout']['description'] = 3
        return result
    bridge.answer = changed
    with pytest.raises(klayout.KLayoutError):
        klayout.create_artifact(*fixture(tmp_path))
    assert not any(r['method'] == 'tools/call' for r in bridge.requests)


def test_generated_reopen_verifier_rejects_duplicate_layer_before_polygon_access(tmp_path):
    from qumus2.records.device_design import layout_payload

    payload = layout_payload(declared_spec())
    code = klayout._script(payload, tmp_path, '0'*32, 'owned', [0, 0, 1, 1])
    node = next(n for n in ast.parse(code).body if isinstance(n, ast.FunctionDef) and n.name == 'verify')
    ns = {'payload': payload}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<generated-readback-control>', 'exec'), ns)
    cell = SimpleNamespace(name='QUMUS_DEVICE', each_inst=lambda: iter(()))
    layers = [i['layer'] for i in payload['layers']]
    duplicate = SimpleNamespace(dbu=.001, cells=lambda: 1, each_top_cell=lambda: iter([cell]),
        top_cell=lambda: cell, layer_indices=lambda: iter([*layers, layers[0]]))
    with pytest.raises(ValueError, match='inventory'):
        ns['verify'](duplicate)
