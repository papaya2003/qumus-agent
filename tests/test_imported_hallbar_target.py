"""Declared R019-format unit data; no owner GDS, bench, model or live MCP."""
from __future__ import annotations

import ast
import hashlib
import json
from types import SimpleNamespace

import pytest
from test_device_design_pose_screen import VERIFICATION, declared_spec

from qumus2.agents.klayout import _script
from qumus2.core.polygons import area_um2, rectangle
from qumus2.records.device_design import (
    FULL_CONTACT_POLICY,
    DeviceDesignSpec,
    ImportedHallBarTarget,
    layout_payload,
    parse_hallbar_target,
    screen_design_pose_window,
    target_contact_coverage,
    target_import_binding,
    target_metal_primitives,
)
from qumus2.testbench.hallbar import build_imported_hallbar_target, load_imported_hallbar_target


def declared_import(*, source_bytes=b'UNIT ONLY; NOT ACTUAL GDS'):
    """Analytic primitive semantics, not a donor/stock or physical native fixture."""
    parts = []
    for index in range(34):
        x, y = 100000+index*2000, 100000
        parts.append({'index': index, 'kind': 'box', 'box_dbu': [x, y, x+1000, y+1000]})
    parts[0] = {'index': 0, 'kind': 'polygon',
                'points_dbu': [[100000,100000],[101000,100000],[100000,101000]]}
    for index, x, y, direction in ((18,510000,500000,1),(19,510000,510000,1),
            (20,510000,490000,1),(21,490000,500000,-1),(22,490000,510000,-1),
            (23,490000,490000,-1)):
        end = x+direction*5000
        parts[index] = {'index':index,'kind':'path','points_dbu':[[x,y],[end,y]],
            'width_dbu':1000,'bgn_ext':0,'end_ext':0,
            'outline_dbu':[[min(x,end),y-500],[max(x,end),y-500],
                           [max(x,end),y+500],[min(x,end),y+500]]}
    parts[32] = {'index':32,'kind':'box','box_dbu':[499000,479000,501000,481000]}
    parts[33] = {'index':33,'kind':'box','box_dbu':[499000,519000,501000,521000]}
    return build_imported_hallbar_target(primitives=parts,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        owner_instruction_id='declared-unit-owner-instruction', owner_instruction_sha256='a'*64,
        size_mm=(1.,1.))


def imported_spec(target=None):
    body = declared_spec().model_dump(mode='json')
    body.update(target=(target or declared_import()).model_dump(mode='json'), overlap_rule=FULL_CONTACT_POLICY)
    return DeviceDesignSpec.model_validate(body)


def test_legacy_target_and_layout_serialization_not_promoted():
    spec = declared_spec()
    dump = spec.target.model_dump(mode='json')
    assert set(dump) == {'version','source','reference','units','frame','size_mm','contacts','receiving_layers_top_first'}
    assert parse_hallbar_target(dump).model_dump(mode='json') == dump
    assert target_import_binding(spec.target) is None
    payload = layout_payload(spec)
    assert set(payload) == {'dbu_um','top_cell','layers'}
    assert len(payload['layers']) == 10
    assert all(set(item) == {'name','layer','points_dbu'} for item in payload['layers'])


def test_full_original_metal_and_continuous_fixed_ends():
    target = declared_import()
    assert isinstance(parse_hallbar_target(target.model_dump(mode='json')), ImportedHallBarTarget)
    assert len(target_metal_primitives(target)) == 34
    assert {c.role for c in target.contacts} == {c.id for c in target.contacts}
    assert all(area_um2(c.points_mm) == pytest.approx(2.) for c in target.contacts if c.role.startswith('voltage'))
    assert target_contact_coverage(target, rectangle(.06,.08,(.5,.5)))['status'] == 'pass'
    assert target_import_binding(target)['target_version'] == 'hallbar-target-gds-v2'
    payload = layout_payload(imported_spec(target))
    assert [item['layer'] for item in payload['layers']] == [1,2,3]
    assert len(payload['layers'][2]['polygons_dbu']) == 34
    assert payload['layers'][2]['polygons_dbu'] == [[list(p) for p in part.outline_dbu] for part in target.metal_primitives]
    assert payload['contact_regions_mm'] == [{'id':c.id,'role':c.role,'points_mm':[list(p) for p in c.points_mm]} for c in target.contacts]
    assert payload['viewport_box_um'][0] > 400  # Do not zoom out to unrelated large pads.


@pytest.mark.parametrize('change', ['shrink_end','role_swap','width','inner_endpoint','missing_part'])
def test_end_recipe_or_original_identity_tamper_refuses(change):
    value = declared_import().model_dump(mode='json')
    if change == 'shrink_end':
        value['contacts'][2]['points_mm'] = rectangle(.0002,.0002,(.4905,.51))
    elif change == 'role_swap':
        value['contacts'][2]['source_primitive_index'] = 19
    elif change == 'width':
        value['metal_primitives'][22]['width_dbu'] = 500
    elif change == 'inner_endpoint':
        value['metal_primitives'][22]['points_dbu'].reverse()
    else:
        value['metal_primitives'].pop()
    with pytest.raises(ValueError):
        parse_hallbar_target(value)


def test_area_only_contact_witness_cannot_waive_complete_ends():
    target = declared_import()
    outline = rectangle(.021,.042,(.5,.5))
    result = target_contact_coverage(target, outline, minimum_overlap_um=.001)
    assert result['status'] == 'fail'
    assert result['contacts']['voltage_left_1']['intersection_area_um2'] > 0
    assert not result['contacts']['voltage_left_1']['contained']
    with pytest.raises(ValueError, match='complete imported contact'):
        body = imported_spec().model_dump(mode='json')
        body['sources'][1]['outline_mm'] = rectangle(.021,.042)
        DeviceDesignSpec.model_validate(body)


def test_imported_pose_window_has_strict_containment_and_legacy_policy_not_accepted():
    spec = imported_spec()
    assert screen_design_pose_window(spec, verification=VERIFICATION).status == 'pass'
    body = spec.model_dump(mode='json')
    body['sources'][1]['outline_mm'] = rectangle(.026,.044)  # Nominal full ends, insufficient allowed-pose margin.
    assert screen_design_pose_window(DeviceDesignSpec.model_validate(body), verification=VERIFICATION).status == 'fail'
    body['overlap_rule'] = 'intersection_area_um2 >= contact_overlap_um ** 2'
    with pytest.raises(ValueError, match='policy'):
        DeviceDesignSpec.model_validate(body)


def test_continuous_diagonal_end_is_not_replaced_by_a_rounded_contact_region():
    target = declared_import()
    parts = [part.model_dump(mode='json') for part in target.metal_primitives]
    parts[18].update(points_dbu=[[510000,500000],[515000,501000]],
        outline_dbu=[[509902,500490],[510098,499510],[515098,500510],[514902,501490]])
    target = build_imported_hallbar_target(primitives=parts,
        source_sha256=target.import_receipt.source_sha256,
        owner_instruction_id=target.import_receipt.owner_instruction_id,
        owner_instruction_sha256=target.import_receipt.owner_instruction_sha256,
        size_mm=target.size_mm)
    right = max(x for contact in target.contacts for x, _ in contact.points_mm)
    assert right*1e6 == pytest.approx(512059.16135138186)
    assert round(right*1e6) < right*1e6
    # This complete nominal Gr contains the continuous cut, but rounding its
    # right edge removes part of that cut. Rounded contact metadata would pass.
    gr = ((.47,.478),(right+.05e-6,.478),(right+.05e-6,.522),(.47,.522))
    body = imported_spec(target).model_dump(mode='json')
    body['sources'][1]['outline_mm'] = [[x-.5,y-.5] for x,y in gr]
    spec = DeviceDesignSpec.model_validate(body)
    assert target_contact_coverage(target, gr)['status'] == 'pass'
    with pytest.raises(ValueError, match='quantization loses complete original contact ends'):
        layout_payload(spec)


def test_bundle_read_only_and_immutable_workspace_copy(tmp_path):
    target = declared_import()
    bundle, workspace = tmp_path/'bundle', tmp_path/'workspace'
    bundle.mkdir()
    (bundle/'target.json').write_text(json.dumps(target.model_dump(mode='json')))
    (bundle/'electrode-layout.gds').write_bytes(b'UNIT ONLY; NOT ACTUAL GDS')
    assert load_imported_hallbar_target(bundle) == target and not workspace.exists()
    assert load_imported_hallbar_target(bundle, workspace=workspace) == target
    assert load_imported_hallbar_target(bundle, workspace=workspace) == target
    with pytest.raises(ValueError, match='instruction'):
        load_imported_hallbar_target(bundle, approval_reference='wrong')
    saved = workspace/'targets'/target.import_receipt.source_sha256/'target.json'
    saved.write_text('{}')
    with pytest.raises(ValueError, match='immutable'):
        load_imported_hallbar_target(bundle, workspace=workspace)


def _original_source_stub(target, *, change=None):
    def polygon(points):
        return SimpleNamespace(each_point_hull=lambda: iter(SimpleNamespace(x=x,y=y) for x,y in points))
    shapes = []
    for part in target.metal_primitives:
        if part.kind == 'path':
            path = SimpleNamespace(width=part.width_dbu+(1 if change == 'width' and part.index == 18 else 0),
                bgn_ext=part.bgn_ext,end_ext=part.end_ext,
                each_point=lambda part=part: iter(SimpleNamespace(x=x,y=y) for x,y in part.points_dbu),
                polygon=lambda part=part: polygon(part.outline_dbu))
            shape = SimpleNamespace(path=path,is_path=lambda:True,is_box=lambda:False,is_polygon=lambda:False)
        elif part.kind == 'box':
            x0,y0,x1,y1 = part.box_dbu
            box = SimpleNamespace(left=x0,bottom=y0,right=x1,top=y1,points=part.outline_dbu)
            shape = SimpleNamespace(box=box,is_path=lambda:False,is_box=lambda:True,is_polygon=lambda:False)
        else:
            shape = SimpleNamespace(polygon=polygon(part.points_dbu),
                is_path=lambda:False,is_box=lambda:False,is_polygon=lambda:True)
        shapes.append(shape)
    if change == 'endpoint':
        shapes[18].path.each_point = lambda: iter(SimpleNamespace(x=x,y=y)
            for x,y in reversed(target.metal_primitives[18].points_dbu))
    elif change == 'extension':
        shapes[18].path.bgn_ext = 1
    elif change == 'box':
        shapes[32].box.left += 1
    elif change == 'polygon':
        shapes[0].polygon = polygon([[100000,100000],[101001,100000],[100000,101000]])
    elif change == 'outline':
        shapes[18].path.polygon = lambda: polygon([[510000,499500],[515001,499500],
                                                 [515001,500500],[510000,500500]])
    elif change == 'kind':
        shapes[18].is_path = lambda: False
    elif change == 'missing':
        shapes.pop()
    cell = SimpleNamespace(name='QUMUS_DEVICE',each_inst=lambda:iter(()),
        shapes=lambda _:SimpleNamespace(each=lambda:iter(shapes)))
    layout = SimpleNamespace(dbu=.001,cells=lambda:1,each_top_cell=lambda:iter((cell,)),
        top_cell=lambda:cell,find_layer=lambda *_:0,read=lambda _:None)
    return SimpleNamespace(Layout=lambda:layout,Polygon=lambda box:polygon(box.points))


@pytest.mark.parametrize('change', [None,'width','endpoint','extension','box','polygon','outline','kind','missing','bytes'])
def test_generated_native_source_check_rejects_actual_semantic_mismatch(tmp_path, change):
    """Execute just source validation against a typed pya double; no native GDS claim."""
    target = declared_import()
    path = tmp_path/'electrode-layout.gds'
    path.write_bytes(b'changed bytes' if change == 'bytes' else b'UNIT ONLY; NOT ACTUAL GDS')
    payload = layout_payload(imported_spec(target))
    tree = ast.parse(_script(payload,tmp_path,'0'*32,'owned',[0,0,1,1],path))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {'canonical','polygon_points','item_polygons'}]
    selected += [node for node in tree.body if isinstance(node, ast.If) and ast.unparse(node.test) == "'source_target' in payload"]
    assert len(selected) == 4
    ns = {'payload':payload,'source_gds':str(path),'pathlib':__import__('pathlib'),'hashlib':hashlib,
          'pya':_original_source_stub(target,change=change)}
    code = compile(ast.Module(body=selected,type_ignores=[]),'<offline-source-check>','exec')
    if change is not None:
        with pytest.raises(ValueError, match='Original'):
            exec(code,ns)
    else:
        exec(code,ns)


@pytest.mark.parametrize('change', [None,'missing','xor','decomposition','instance'])
def test_generated_export_verifier_checks_xor_and_all_original_parts(tmp_path, change):
    """Exercise verifier branches with a typed pya double; not native GDS evidence."""
    payload = layout_payload(imported_spec())
    tree = ast.parse(_script(payload,tmp_path,'0'*32,'owned',[0,0,1,1]))
    names = {'canonical','polygon_points','item_polygons','item_regions','native_polygon','verify'}
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]

    class Polygon:
        def __init__(self, points):
            self.points = tuple(points)

        def each_point_hull(self):
            return iter(SimpleNamespace(x=x,y=y) for x,y in self.points)

        def area(self):
            return 1

        def insert_hole(self, points):
            raise AssertionError('electrode primitives carry no holes')

    class Region:
        def __init__(self, polygons=()):
            self.polygons = list(polygons)

        def insert(self, polygon):
            self.polygons.append(polygon)

        def __xor__(self, other):
            # The decomposition control models equal unions with different
            # primitive boundaries, which must still be refused separately.
            same = change == 'decomposition' or sorted(p.points for p in self.polygons) == sorted(p.points for p in other.polygons)
            return SimpleNamespace(is_empty=lambda: same)

    polygons = {item['layer']: [list(map(tuple, points)) for points in item['polygons_dbu']]
                for item in payload['layers']}
    if change == 'missing':
        polygons[3].pop()
    elif change in {'xor','decomposition'}:
        x,y = polygons[3][0][0]
        polygons[3][0][0] = (x+1,y)
    shapes = {layer: [SimpleNamespace(polygon=Polygon(points),is_polygon=lambda:True,is_box=lambda:False)
                      for points in parts] for layer,parts in polygons.items()}
    cell = SimpleNamespace(name='QUMUS_DEVICE',
        each_inst=lambda: iter((object(),) if change == 'instance' else ()),
        shapes=lambda layer: SimpleNamespace(each=lambda:iter(shapes[layer])),
        begin_shapes_rec=lambda layer: [shape.polygon for shape in shapes[layer]])
    candidate = SimpleNamespace(dbu=.001,cells=lambda:1,each_top_cell=lambda:iter((cell,)),
        top_cell=lambda:cell,layer_indices=lambda:iter(polygons),
        get_info=lambda layer:SimpleNamespace(layer=layer,datatype=0),find_layer=lambda layer,_:layer)
    ns = {'payload':payload,'pya':SimpleNamespace(Polygon=Polygon,Point=lambda x,y:(x,y),Region=Region)}
    exec(compile(ast.Module(body=selected,type_ignores=[]),'<offline-export-check>','exec'),ns)
    if change is None:
        ns['verify'](candidate)
    else:
        with pytest.raises(ValueError, match='Saved GDS'):
            ns['verify'](candidate)
