// Resizable columns (Jialei 2026-11-04, "like VS Code"): dragging the gap
// between two windows sets the width of the left list or of the right column
// (--side-w, --lab-w); the centre takes the rest. The gap itself is the grip
// (its own grid column, so the whole gap and only the gap answers the mouse)
// and shows three dots that light up on hover and while dragging. Widths are
// remembered per browser; double-click the gap to return to the default.
// Phone layout (one column at a time) has no gaps and ignores the widths.

const KEY = 'qumus.panes';
const LIMITS = { side: [180, 640], lab: [280, 980] };
const MIN_CENTRE = 380;
const PHONE_MAX = 860;

function load() {
  try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch { return {}; }
}
function save(widths) {
  try { localStorage.setItem(KEY, JSON.stringify(widths)); } catch { /* private window */ }
}

export function initPanes(app = document.getElementById('app')) {
  if (!app) return;
  let widths = load();
  const phone = () => window.innerWidth <= PHONE_MAX;
  const apply = () => {
    for (const key of ['side', 'lab']) {
      if (!phone() && widths[key]) app.style.setProperty(`--${key}-w`, `${widths[key]}px`);
      else app.style.removeProperty(`--${key}-w`);
    }
  };
  const clamp = (key, w) => {
    const [lo, hi] = LIMITS[key];
    const other = key === 'side' ? 'lab' : 'side';
    const otherEl = document.getElementById(other);
    const otherW = otherEl && otherEl.offsetParent ? otherEl.getBoundingClientRect().width : 0;
    const act = document.getElementById('act');
    const chrome = (act ? act.getBoundingClientRect().right : 68) + 2 * 8 + 6;  // nav strip, two gaps, edge
    const room = window.innerWidth - chrome - otherW - MIN_CENTRE;
    return Math.round(Math.max(lo, Math.min(hi, room, w)));
  };
  for (const key of ['side', 'lab']) {
    const pane = document.getElementById(key);
    if (!pane) continue;
    const handle = document.createElement('div');
    handle.className = `pane-gutter pane-gutter-${key}`;
    handle.title = 'Drag to resize · double-click to reset';
    handle.setAttribute('role', 'separator');
    handle.setAttribute('aria-orientation', 'vertical');
    handle.innerHTML = '<span class="pg-dots" aria-hidden="true"><i></i><i></i><i></i></span>';
    app.appendChild(handle);
    handle.addEventListener('pointerdown', ev => {
      // nothing to resize when the window next to this gap is closed
      if (phone() || ev.button !== 0 || !pane.offsetParent) return;
      ev.preventDefault();
      handle.setPointerCapture(ev.pointerId);
      const x0 = ev.clientX, w0 = pane.getBoundingClientRect().width;
      app.classList.add('resizing');
      handle.classList.add('dragging');
      const move = e => {
        const dx = e.clientX - x0;
        widths[key] = clamp(key, key === 'side' ? w0 + dx : w0 - dx);
        apply();
      };
      const up = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        handle.removeEventListener('pointercancel', up);
        app.classList.remove('resizing');
        handle.classList.remove('dragging');
        save(widths);
        window.dispatchEvent(new Event('resize'));  // drawings refit
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
      handle.addEventListener('pointercancel', up);
    });
    handle.addEventListener('dblclick', () => {
      if (!pane.offsetParent) return;
      delete widths[key]; save(widths); apply();
      window.dispatchEvent(new Event('resize'));
    });
  }
  window.addEventListener('resize', apply);
  apply();
}
