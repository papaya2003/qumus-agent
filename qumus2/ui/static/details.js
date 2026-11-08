// The details of one thing, for the right column (Results, Lab, History): a
// flake, a chip, a device, a stamp, a material slide or tape. The same
// details wherever the thing is clicked; where it is is said in words, never
// with a picture of the lab (Jialei, 2026-10-23).
//
// ctx: { s: the Session the thing belongs to, flake: a FlakeDetail, live() ->
// bool, open(id) another sample, photo(src), filmBy (the stamp form's name),
// changed() redraw }

import { h, imgUrl, tapeText } from './ui.js';
import { bestPhoto, thickText, shortId } from './flakes.js';
import { materialMetalSummary } from './materialmetal.js';
import { STAMP_WORD, STAMP_TAG, REFILMABLE, layerStack, whereText, materialReview, materialReviewText, samplePhoto } from './inventory.js';

const KIND = { chip: 'Chip', device: 'Device', stamp: 'Stamp', carrier: 'Mother tape', tape: 'Daughter tape', flake: 'Flake' };

export function sampleDetail(x, ctx) {
  const s = ctx.s, inv = s.inv;
  if (x.kind === 'flake') {
    return { title: `${x.props.material || ''} flake ${shortId(x.id)}`.trim(), sub: x.props.chip_id || '', node: ctx.flake.render(x) };
  }
  const kv = [], extra = [];
  const photo = samplePhoto(x);
  if (photo) extra.push(h('figure', { class: 'sample-photo' },
    h('img', { src: imgUrl(photo.src), alt: `${x.id} ${photo.caption}`, onclick: () => ctx.photo(photo.src) }),
    h('figcaption', { text: photo.caption })));
  if (x.kind === 'chip') {
    const mm = materialMetalSummary(x, s.events);
    if (mm) {
      kv.push(['Simulation', mm.status], ['Scope', mm.caption], ['Layers, top first', mm.layers.join(' / ')],
        ['Transfer support', mm.stampFilm], ['Qualitative progress', mm.progress == null ? 'Not measured' : `${(mm.progress * 100).toFixed(0)}%`],
        ['Source flakes', mm.sourceIds.join(', ')]);
      if (mm.releaseImages.length) extra.push(h('div', {class: 'd-sec', text: 'Verified simulated release'}),
        h('div', {class: 'thumbgrid'}, mm.releaseImages.map(src => h('button', {class: 'tg', onclick: () => ctx.photo(src)},
          h('img', {src: imgUrl(src), alt: 'Simulated stack after release', loading: 'lazy'})))));
    }
    const fl = inv.flakesOf(x.id);
    if (!mm) kv.push(['Material', x.props.material || (inv.isTarget(x) ? 'pre-patterned electrodes' : 'none yet')]);
    kv.push(['Status', String(x.status || '').replace(/_/g, ' ')]);
    if (fl.length) kv.push(['Flakes', `${fl.length} found · ${fl.filter(f => bestPhoto(f)).length} photographed`]);
    const devs = inv.of('device').filter(d => d.location === x.id);
    if (devs.length) kv.push(['Devices', devs.map(d => d.id).join(', ')]);
    if (fl.length) {
      const shot = fl.filter(f => bestPhoto(f)).sort((a, b) => (b.props.area_um2 || 0) - (a.props.area_um2 || 0));
      extra.push(h('div', { class: 'd-sec', text: `Flakes on it · ${fl.length}` }),
        h('div', { class: 'thumbgrid' }, shot.slice(0, 12).map(f => h('button', { class: 'tg', title: `${f.id} · ${thickText(f.props.material, f.props.thickness_label)}`,
          onclick: () => ctx.open(f.id) }, h('img', { src: imgUrl(bestPhoto(f)), alt: f.id, loading: 'lazy' }), h('small', { class: 'mono', text: shortId(f.id) })))),
        shot.length > 12 ? h('div', { class: 'muted small', text: `and ${shot.length - 12} more with photos` }) : null);
    }
  } else if (x.kind === 'device') {
    const layers = x.props.layers_top_first || [];
    kv.push(['Layers', `${layers.length}, top first`]);
    kv.push(['Chip', `${x.location}${inv.samples.get(x.location)?.props?.electrodes ? ' (pre-patterned electrodes)' : ''}`]);
    extra.push(h('div', { class: 'd-sec', text: 'Layers, top first' }), layerStack(x, inv, id => s.roleOf(id)),
      h('div', { class: 'thumbgrid wide' }, layers.map(id => {
        const f = inv.samples.get(id), src = f ? bestPhoto(f) : null;
        return h('button', { class: 'tg', title: `${id} · details`, onclick: () => ctx.open(id) },
          src ? h('img', { src: imgUrl(src), alt: id, loading: 'lazy' }) : h('span', { class: 'fnone', text: 'no photo' }),
          h('small', {}, h('span', { class: 'mono', text: shortId(id) }), ` ${s.roleOf(id) || (f && f.props.material) || ''}`));
      })));
  } else if (x.kind === 'carrier') {
    const review = materialReview(x);
    kv.push(['Material label', x.props.material || 'Unknown'], ['Source', 'Inventory label; not inferred from camera images'],
      ['Human review', materialReviewText(x)]);
    if (review.note) kv.push(['Review note', review.note]);
    extra.push(h('p', {class: 'muted small', text: 'Check the tape’s physical label or preparation record. Similar-looking tapes cannot be identified reliably from the overview camera.'}));
    if (ctx.live()) extra.push(materialForm(x, ctx));
    else extra.push(h('p', {class: 'muted small', text: 'Read-only recording. Human review is available in the live Lab inventory.'}));
  } else if (x.kind === 'stamp') {
    kv.push(['Status', h('span', { class: 'tag ' + (STAMP_TAG[x.status] || ''), text: STAMP_WORD[x.status] || x.status })]);
    for (const f of inv.stampFacts(x)) kv.push(['', f]);
    if (ctx.live() && REFILMABLE.has(x.status)) extra.push(filmForm(x, ctx));
    else if (REFILMABLE.has(x.status)) extra.push(h('p', { class: 'muted small', text: 'A person reports a new PC film here on the live page (not in a recording).' }));
  } else {
    if (x.props.material) kv.push(['Material', x.props.material]);
    if (x.props.roll_left_mm != null) kv.push(['Tape left', tapeText(x.props.roll_left_mm)]);
    if (x.props.uses) kv.push(['Used', `${x.props.uses}×`]);
    kv.push(['Status', String(x.status || '').replace(/_/g, ' ')]);
  }
  kv.push(['Now', whereText(x, inv) || '–']);
  const hist = s.sampleHistory(x.id);
  return {
    title: x.id, sub: KIND[x.kind] || x.kind,
    node: h('div', { class: 'detail-body' },
      h('dl', { class: 'kv' }, kv.flatMap(([k, v]) => [h('dt', { text: k }), h('dd', {}, v)])),
      ...extra,
      h('div', { class: 'd-sec', text: 'History in the lab' }),
      h('ol', { class: 'fl-hist' }, hist.length ? hist.map(e => h('li', {}, h('span', { class: 'n mono', text: e.when || '' }), h('i', { class: e.cls || '' }), h('span', { text: e.text })))
        : [h('li', { class: 'muted history-empty', text: 'Nothing recorded about it in this run.' })])),
  };
}

// "New PC film": only after the film is on and the stamp is back in its slot (live)
function filmForm(x, ctx) {
  const by = h('input', { type: 'text', placeholder: 'Your name (optional)', maxlength: '80', value: ctx.filmBy || '',
    oninput: ev => { ctx.filmBy = ev.target.value; } });
  const go = h('button', { class: 'inv-btn primary', text: 'Confirm new PC film', onclick: async () => {
    go.disabled = true; go.textContent = 'Saving…';
    await ctx.s.inv.newFilm(x.id, ctx.filmBy || '');
    ctx.changed();
  } });
  by.onkeydown = ev => { if (ev.key === 'Enter') go.click(); };
  return h('div', { class: 'inv-film' },
    h('div', { class: 'inv-film-t', text: `Put a new PC film on ${x.id}? Only after the film is on and the stamp is back in its slot (${whereText(x, ctx.s.inv) || 'its slot'}). It is ${STAMP_WORD[x.status] || x.status} now; it will be new.` }),
    h('div', { class: 'inv-film-row' }, by, go));
}

function materialForm(x, ctx) {
  const name = h('input', {type: 'text', placeholder: 'Name of the person reviewing', 'aria-label': 'Material reviewer', maxlength: 80});
  const note = h('textarea', {placeholder: 'Physical label / preparation record (optional)', 'aria-label': 'Material review note', maxlength: 500, rows: 2});
  const save = async status => {
    confirm.disabled = uncertain.disabled = true;
    const ok = await ctx.s.inv.reviewMaterial(x.id, name.value.trim(), status, note.value.trim());
    if (ok) ctx.changed?.();
    else confirm.disabled = uncertain.disabled = !name.value.trim();
  };
  const confirm = h('button', {class: 'inv-btn primary', text: `Confirm ${x.props.material || 'material'} label`, disabled: true, onclick: () => save('confirmed')});
  const uncertain = h('button', {class: 'inv-btn', text: 'Identity uncertain', disabled: true, onclick: () => save('uncertain')});
  name.addEventListener('input', () => { confirm.disabled = uncertain.disabled = !name.value.trim(); });
  return h('div', {class: 'material-review-form'}, h('b', {text: 'Human material review'}), name, note,
    h('div', {class: 'material-review-actions'}, confirm, uncertain),
    h('small', {class: 'muted', text: 'Confirm only after checking the label or preparation record. Mark uncertain if it cannot be verified; the tape will be excluded from exfoliation.'}));
}
