/**
 * web/js/charts.js
 * Монохромные SVG-графики без зависимостей — по мотивам amicro «mono charts»:
 * карточка с маленькой подписью и чипом, крупное число с приглушённой единицей,
 * сам график со скруглёнными концами и подвал из двух подписей.
 *
 * Цвет берётся из `currentColor` и прозрачности, акцент — из CSS-переменной,
 * поэтому графики следуют теме приложения. Функции возвращают строки HTML/SVG.
 */

const NS = 'http://www.w3.org/2000/svg';

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

const r2 = (n) => Math.round(n * 100) / 100;

/**
 * Карточка графика: подпись и чип сверху, крупное число, график, две подписи внизу.
 * chart — готовая разметка (SVG или HTML).
 */
export function chartCard({ label, chip = '', value = '', unit = '', chart = '', footLeft = '', footRight = '', extraClass = '' }) {
  return `
    <div class="mono-card ${extraClass}">
      <div class="mono-card-head">
        <span class="mono-label">${esc(label)}</span>
        ${chip ? `<span class="mono-chip">${esc(chip)}</span>` : ''}
      </div>
      ${value !== '' ? `<div class="mono-value">${esc(value)}${unit ? `<span class="mono-unit">${esc(unit)}</span>` : ''}</div>` : ''}
      <div class="mono-chart">${chart}</div>
      ${(footLeft || footRight) ? `
        <div class="mono-foot">
          <span>${esc(footLeft)}</span>
          <span>${esc(footRight)}</span>
        </div>` : ''}
    </div>`;
}

/**
 * Горизонтальная «пилюля» шанса: дорожка и заливка со скруглёнными краями.
 * pct — 0..100. highlight — акцентный цвет вместо монохромного.
 */
export function pillBar(pct, { highlight = false, muted = false } = {}) {
  const w = Math.max(0, Math.min(100, Number(pct) || 0));
  // Совсем маленький шанс всё равно виден точкой, а не пропадает.
  const fill = w > 0 ? Math.max(w, 3) : 0;
  return `
    <span class="mono-pill ${highlight ? 'hl' : ''} ${muted ? 'muted' : ''}">
      <span class="mono-pill-fill" style="width:${r2(fill)}%"></span>
    </span>`;
}

/**
 * Вертикальные пилюли-столбцы: дорожка на всю высоту и заливка по значению.
 * items: [{value, label, highlight}] — value в одних единицах, шкала по максимуму.
 */
export function pillColumns(items, { width = 300, height = 96, gap = 10 } = {}) {
  const list = (items || []).filter(i => Number.isFinite(Number(i.value)));
  if (!list.length) return '';
  const n = list.length;
  const colW = Math.max(6, Math.min(26, (width - gap * (n - 1)) / n));
  const total = colW * n + gap * (n - 1);
  const x0 = (width - total) / 2;
  const max = Math.max(...list.map(i => Number(i.value)), 1e-9);
  const cols = list.map((item, idx) => {
    const x = x0 + idx * (colW + gap);
    const h = Math.max(colW, (Number(item.value) / max) * height);
    const cls = item.highlight ? 'mono-accent' : 'mono-ink';
    return `
      <rect x="${r2(x)}" y="0" width="${r2(colW)}" height="${height}" rx="${r2(colW / 2)}" class="mono-track"/>
      <rect x="${r2(x)}" y="${r2(height - h)}" width="${r2(colW)}" height="${r2(h)}" rx="${r2(colW / 2)}" class="${cls}">
        <title>${esc(item.label ?? '')}</title>
      </rect>`;
  }).join('');
  return `<svg class="mono-svg" viewBox="0 0 ${width} ${height}" xmlns="${NS}" role="img">${cols}</svg>`;
}

/**
 * Монотонный кубический сплайн (Fritsch–Carlson): кривая гладкая, но не
 * «перелетает» за соседние точки — шанс не рисуется ниже нуля или выше пика.
 */
function monotonePath(pts) {
  const n = pts.length;
  if (n === 0) return '';
  if (n === 1) return `M${r2(pts[0][0])},${r2(pts[0][1])}`;
  const dx = [], slope = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(pts[i + 1][0] - pts[i][0]);
    slope.push(dx[i] === 0 ? 0 : (pts[i + 1][1] - pts[i][1]) / dx[i]);
  }
  const m = [slope[0]];
  for (let i = 1; i < n - 1; i++) {
    m.push(slope[i - 1] * slope[i] <= 0 ? 0 : (slope[i - 1] + slope[i]) / 2);
  }
  m.push(slope[n - 2]);
  for (let i = 0; i < n - 1; i++) {
    if (slope[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
    const a = m[i] / slope[i], b = m[i + 1] / slope[i];
    const s = a * a + b * b;
    if (s > 9) {
      const t = 3 / Math.sqrt(s);
      m[i] = t * a * slope[i];
      m[i + 1] = t * b * slope[i];
    }
  }
  let d = `M${r2(pts[0][0])},${r2(pts[0][1])}`;
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    d += ` C${r2(pts[i][0] + h)},${r2(pts[i][1] + m[i] * h)} ${r2(pts[i + 1][0] - h)},${r2(pts[i + 1][1] - m[i + 1] * h)} ${r2(pts[i + 1][0])},${r2(pts[i + 1][1])}`;
  }
  return d;
}

function parseTime(t) {
  if (typeof t === 'number') return t;
  // «YYYY-MM-DD HH:MM:SS» в МСК — для оси нужен только порядок и интервалы.
  const ms = Date.parse(String(t || '').replace(' ', 'T'));
  return Number.isFinite(ms) ? ms : NaN;
}

/**
 * Сглаженные линии по времени. series: [{name, points: [{t, v}], highlight}].
 * Ось Y — общая для всех линий, от нуля до максимума с запасом. Первая линия с
 * highlight рисуется акцентом и получает точку на последнем значении.
 */
export function splineChart(series, { width = 320, height = 120, pad = 6, area = true } = {}) {
  const lines = (series || [])
    .map(s => ({ ...s, pts: (s.points || []).map(p => [parseTime(p.t), Number(p.v)]).filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1])) }))
    .filter(s => s.pts.length > 0);
  if (!lines.length) return '';
  const all = lines.flatMap(s => s.pts);
  let tMin = Math.min(...all.map(p => p[0]));
  let tMax = Math.max(...all.map(p => p[0]));
  if (tMax === tMin) { tMin -= 1; tMax += 1; }
  const vMax = Math.max(...all.map(p => p[1])) * 1.12 || 1;
  const sx = (t) => pad + ((t - tMin) / (tMax - tMin)) * (width - pad * 2);
  const sy = (v) => height - pad - (v / vMax) * (height - pad * 2);

  // Пунктир сетки — три уровня, как у референса.
  const grid = [0.25, 0.5, 0.75].map(k => {
    const y = r2(pad + k * (height - pad * 2));
    return `<line x1="${pad}" y1="${y}" x2="${width - pad}" y2="${y}" class="mono-grid"/>`;
  }).join('');

  // Линии рисуем от второстепенных к акцентной, чтобы она была сверху.
  const ordered = [...lines].sort((a, b) => Number(Boolean(a.highlight)) - Number(Boolean(b.highlight)));
  const paths = ordered.map((s, idx) => {
    const pts = s.pts.map(([t, v]) => [sx(t), sy(v)]);
    if (pts.length === 1) pts.unshift([pad, pts[0][1]]);
    const d = monotonePath(pts);
    const last = pts[pts.length - 1];
    const cls = s.highlight ? 'mono-line mono-accent-stroke' : 'mono-line';
    const opacity = s.highlight ? 1 : Math.max(0.25, 0.7 - idx * 0.1);
    const fill = s.highlight && area
      ? `<path d="${d} L${r2(last[0])},${height - pad} L${r2(pts[0][0])},${height - pad} Z" class="mono-area"/>`
      : '';
    const dot = s.highlight
      ? `<circle cx="${r2(last[0])}" cy="${r2(last[1])}" r="3.5" class="mono-dot"/>`
      : '';
    return `${fill}<path d="${d}" class="${cls}" style="opacity:${opacity}"><title>${esc(s.name || '')}</title></path>${dot}`;
  }).join('');

  return `<svg class="mono-svg" viewBox="0 0 ${width} ${height}" xmlns="${NS}" role="img" preserveAspectRatio="none">${grid}${paths}</svg>`;
}

/**
 * Кольцо со скруглёнными концами дуг и зазором между сегментами.
 * segments: [{value, label, highlight}]. center — подпись в середине.
 */
export function donutChart(segments, { size = 132, thickness = 14, gap = 0.06, center = '', centerSub = '' } = {}) {
  const segs = (segments || []).filter(s => Number(s.value) > 0);
  const total = segs.reduce((acc, s) => acc + Number(s.value), 0);
  const c = size / 2;
  const r = c - thickness / 2 - 1;
  const track = `<circle cx="${c}" cy="${c}" r="${r2(r)}" class="mono-track-ring" stroke-width="${thickness}" fill="none"/>`;
  let arcs = '';
  if (total > 0) {
    // Скруглённый конец выступает на половину толщины — убираем его из длины дуги.
    const capAngle = thickness / 2 / r;
    let angle = -Math.PI / 2;
    segs.forEach((s, idx) => {
      const sweep = (Number(s.value) / total) * Math.PI * 2;
      const start = angle + capAngle + gap / 2;
      const end = angle + sweep - capAngle - gap / 2;
      angle += sweep;
      const cls = s.highlight ? 'mono-accent-stroke' : 'mono-ink-stroke';
      const opacity = s.highlight ? 1 : Math.max(0.18, 0.75 - idx * 0.14);
      const title = `<title>${esc(s.label ?? '')}</title>`;
      if (segs.length === 1) {
        arcs += `<circle cx="${c}" cy="${c}" r="${r2(r)}" class="${cls}" stroke-width="${thickness}" fill="none">${title}</circle>`;
        return;
      }
      if (end <= start) {
        // Сегмент короче своих концов — рисуем точкой по центру дуги.
        const mid = angle - sweep / 2;
        arcs += `<circle cx="${r2(c + r * Math.cos(mid))}" cy="${r2(c + r * Math.sin(mid))}" r="${r2(thickness / 2 * 0.8)}" class="${s.highlight ? 'mono-accent' : 'mono-ink'}" style="opacity:${opacity}">${title}</circle>`;
        return;
      }
      const x1 = c + r * Math.cos(start), y1 = c + r * Math.sin(start);
      const x2 = c + r * Math.cos(end), y2 = c + r * Math.sin(end);
      const large = end - start > Math.PI ? 1 : 0;
      arcs += `<path d="M${r2(x1)},${r2(y1)} A${r2(r)},${r2(r)} 0 ${large} 1 ${r2(x2)},${r2(y2)}" class="${cls}" stroke-width="${thickness}" stroke-linecap="round" fill="none" style="opacity:${opacity}">${title}</path>`;
    });
  }
  const label = center ? `
    <text x="${c}" y="${c - (centerSub ? 2 : -5)}" text-anchor="middle" class="mono-donut-value">${esc(center)}</text>
    ${centerSub ? `<text x="${c}" y="${c + 14}" text-anchor="middle" class="mono-donut-sub">${esc(centerSub)}</text>` : ''}` : '';
  return `<svg class="mono-svg mono-donut" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" xmlns="${NS}" role="img">${track}${arcs}${label}</svg>`;
}

/** Легенда к кольцу/линиям: точка, имя, значение. */
export function legend(items) {
  return `<div class="mono-legend">${(items || []).map((it, idx) => `
    <div class="mono-legend-row">
      <span class="mono-legend-dot ${it.highlight ? 'hl' : ''}" style="${it.highlight ? '' : `opacity:${Math.max(0.18, 0.75 - idx * 0.14)}`}"></span>
      <span class="mono-legend-name">${esc(it.label)}</span>
      <span class="mono-legend-val">${esc(it.value)}</span>
    </div>`).join('')}</div>`;
}
