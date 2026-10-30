// Представление плана в THREE: рисунок плана на полу и выдавленные элементы по таблице стилей (plan_scene.js).
// Ничего не знает о людях, треках и сокете: получает план, отдаёт группу, которую страница кладёт в сцену.
import { STYLES, elementsFromLayout, extrudeAll } from './plan_scene.js';
import { floorCellsByKind, floorKindAt, paintPatch } from './floor_texture.js';     // пол по комнатам: паркет, линолеум, плитка

const PLAN_PX_PER_M = 160;                   // разрешение рисунка плана на полу: точки пола круглые и мелкие, при 80 пикселях на метр они превращаются в квадраты (было 80)
const FLOOR_OPACITY = 1.0;                   // пол из мелких точек: прозрачность даёт сама сетка, сверху точки непрозрачные
const EDGE_PX = 3;                         // линия контура мебели и окон на рисунке, пикселей
const APPROX_DASH = [12, 8];                 // пунктир контура предмета, положение которого оценено со слов
const JAMB_HALF_M = 0.08;                    // половина длины косяка поперёк стены
const BLOCK_DEPTH_WRITE = false;             // блоки прозрачные: глубину не пишут, порядок задаёт renderOrder

// Гашение стены, на которую камера смотрит прямо: гаснет вся стена целиком,
// если она между камерой и центром сцены и камера смотрит вдоль её нормали; под углом и вдали стена остаётся видна.
const OCC_NONE_M = 1.0, OCC_FULL_M = 3.0;            // метры от центра сцены к камере: ближе первого гашения нет, дальше второго - полное
const vertexShader = `uniform float uPx, uSize, uMin, uMax, uOpacity, uFade, uH, uOcc, uF0, uF1, uFloor; uniform vec3 uCam, uFocus; attribute vec2 aN; varying float vA;
  void main() { vec4 mv = modelViewMatrix * vec4(position, 1.0); gl_Position = projectionMatrix * mv;
    gl_PointSize = clamp(uPx * uSize / -mv.z, uMin, uMax); vA = uOpacity * (1.0 - uFade * position.y / uH);
    if (uOcc > 0.5) {
      float facing = abs(dot(normalize(position.xz - uCam.xz), aN));                       // 1 - камера смотрит строго на стену, 0 - вдоль неё
      float between = smoothstep(${OCC_NONE_M.toFixed(1)}, ${OCC_FULL_M.toFixed(1)}, dot(position - uFocus, normalize(uCam - uFocus)));   // стена на стороне камеры от центра
      vA *= mix(1.0, uFloor, between * smoothstep(uF0, uF1, facing)); } }`;
const fragmentShader = `uniform vec3 uColor; varying float vA;
  void main() { float r = length(gl_PointCoord - 0.5) * 2.0; float a = (1.0 - smoothstep(0.5, 1.0, r)) * vA;
    if (a < 0.02) discard; gl_FragColor = vec4(uColor, a); }`;

export class PlanView {
  constructor(THREE, { uPx, renderer, order, styles = STYLES }) {
    this.THREE = THREE; this.uPx = uPx; this.renderer = renderer; this.order = order; this.styles = styles;
    this.group = new THREE.Group();
    this.view = { uCam: { value: new THREE.Vector3() }, uFocus: { value: new THREE.Vector3() } };   // общие для всех стилей
  }

  setView(camera, focus) {                   // куда смотрит камера: нужно стилям с occlude
    this.view.uCam.value.copy(camera); this.view.uFocus.value.copy(focus);
  }

  clear() {
    for (const c of [...this.group.children]) { this.group.remove(c); c.traverse(o => { o.geometry?.dispose(); o.material?.map?.dispose(); o.material?.dispose?.(); }); }
  }

  build(layout) {
    this.clear();
    const els = elementsFromLayout(layout);
    if (layout.floor) this.group.add(this._plan(layout, els));
    this._points(els);
    this._blocks(els);
    return this.group;
  }

  _plan(layout, els) {                       // рисунок плана: пол, стены с толщиной, проёмы, мебель - из тех же элементов, что и выдавливание
    const T = this.THREE, f = layout.floor, s = PLAN_PX_PER_M, W = f.w * f.cell, H = f.h * f.cell, c = document.createElement('canvas');
    c.width = Math.round(W * s); c.height = Math.round(H * s);
    const g = c.getContext('2d'), u = x => (x - f.x0) * s, v = z => (z - f.z0) * s;
    const rooms = layout.rooms || [];
    for (const [kind, runs] of Object.entries(floorCellsByKind(f, rooms))) {        // каждый тип пола рисуется своим рисунком в границах своих клеток
      const box = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
      g.save(); g.beginPath();
      for (const [r, a, b] of runs) {
        const x = a * f.cell * s, y = r * f.cell * s, w = (b - a) * f.cell * s + 1, h = f.cell * s + 1;
        g.rect(x, y, w, h);
        box.x0 = Math.min(box.x0, x); box.y0 = Math.min(box.y0, y); box.x1 = Math.max(box.x1, x + w); box.y1 = Math.max(box.y1, y + h);
      }
      g.clip(); paintPatch(g, kind, box, s, f.x0, f.z0); g.restore();
    }
    const at = (rect, draw) => { g.save(); g.translate(u(rect.cx), v(rect.cz)); g.rotate(rect.a); draw(); g.restore(); };
    const box = (rect, fn) => at(rect, () => fn(-rect.w / 2 * s, -rect.d / 2 * s, rect.w * s, rect.d * s));
    const byKind = k => els.filter(e => e.kind === k);
    for (const e of [...byKind('wall')]) { const p = this.styles[e.style]?.plan; if (p?.fill) { g.fillStyle = p.fill; box(e.rect, (x, y, w, d) => g.fillRect(x, y, w, d)); } }
    const doorBounds = (rect, cut) => { const half = Math.hypot(rect.w, cut) / 2 * s + 0.1 * s, cx = u(rect.cx), cz = v(rect.cz); return { x0: cx - half, y0: cz - half, x1: cx + half, y1: cz + half }; };
    for (const e of byKind('door')) {        // проём: стена стёрта до пола, по краям косяки; пол в проёме тот же рисунок и с теми же точками, не сплошная заливка
      const p = this.styles.door.plan, cut = p.cut ?? e.rect.d;
      at(e.rect, () => { g.beginPath(); g.rect(-e.rect.w / 2 * s, -cut / 2 * s, e.rect.w * s, cut * s); });      // контур проёма остаётся текущим путём после restore
      g.save(); g.clip(); paintPatch(g, floorKindAt(rooms, e.rect.cx, e.rect.cz), doorBounds(e.rect, cut), s, f.x0, f.z0); g.restore();
      at(e.rect, () => {
        g.strokeStyle = p.jamb; g.lineWidth = EDGE_PX; g.beginPath();
        for (const side of [-1, 1]) { g.moveTo(side * e.rect.w / 2 * s, -JAMB_HALF_M * s); g.lineTo(side * e.rect.w / 2 * s, JAMB_HALF_M * s); }
        g.stroke();
      });
    }
    for (const e of byKind('window')) { const p = this.styles.window.plan; g.strokeStyle = p.edge; g.lineWidth = EDGE_PX; at(e.rect, () => g.strokeRect(-e.rect.w / 2 * s, -p.t / 2 * s, e.rect.w * s, p.t * s)); }
    for (const e of byKind('seat')) {          // сиденье: эллипс по габаритам, без объёма
      const p = this.styles.seat.plan; g.fillStyle = p.fill; g.strokeStyle = p.edge; g.lineWidth = EDGE_PX;
      at(e.rect, () => { g.beginPath(); g.ellipse(0, 0, e.rect.w / 2 * s, e.rect.d / 2 * s, 0, 0, 2 * Math.PI); g.fill(); g.stroke(); });
    }
    for (const e of byKind('furniture')) {
      const p = this.styles.furniture.plan;
      g.fillStyle = p.fill; g.strokeStyle = p.edge; g.lineWidth = EDGE_PX; g.setLineDash(e.approx ? APPROX_DASH : []);   // со слов человека - пунктиром
      box(e.rect, (x, y, w, d) => { g.fillRect(x, y, w, d); g.strokeRect(x, y, w, d); });
      g.setLineDash([]);
    }
    const tex = new T.CanvasTexture(c); tex.colorSpace = T.SRGBColorSpace; tex.anisotropy = this.renderer.capabilities.getMaxAnisotropy();
    const plane = new T.Mesh(new T.PlaneGeometry(W, H), new T.MeshBasicMaterial({ map: tex, transparent: true, opacity: FLOOR_OPACITY, depthWrite: false }));
    plane.rotation.x = -Math.PI / 2; plane.position.set(f.x0 + W / 2, 0, f.z0 + H / 2); plane.renderOrder = this.order.floor;
    return plane;
  }

  _points(els) {                             // по одному Points на стиль: у каждого свои размер, прозрачность и затухание вверх
    const T = this.THREE, maxH = Math.max(1, ...els.map(e => e.h));
    const normals = {};
    for (const [name, pos] of Object.entries(extrudeAll(els, this.styles, normals))) {
      const st = this.styles[name], geo = new T.BufferGeometry();
      geo.setAttribute('position', new T.BufferAttribute(pos, 3));
      if (st.occlude) geo.setAttribute('aN', new T.BufferAttribute(normals[name], 2));
      const mat = new T.ShaderMaterial({ vertexShader, fragmentShader, transparent: true, depthWrite: false, blending: T.AdditiveBlending,
        uniforms: { uPx: this.uPx, uSize: { value: st.size }, uMin: { value: st.px[0] }, uMax: { value: st.px[1] }, uOpacity: { value: st.opacity },
                    uFade: { value: st.fade }, uH: { value: maxH }, uColor: { value: new T.Color(...st.color) },
                    uOcc: { value: st.occlude ? 1 : 0 }, uF0: { value: st.occlude?.f0 ?? 0 }, uF1: { value: st.occlude?.f1 ?? 1 }, uFloor: { value: st.occlude?.floor ?? 1 }, ...this.view } });
      const pts = new T.Points(geo, mat); pts.frustumCulled = false; pts.renderOrder = this.order.wall; this.group.add(pts);
    }
  }

  _blocks(els) {                             // режим blocks: тот же элемент полупрозрачным ящиком; включается строкой в таблице стилей
    const T = this.THREE;
    for (const e of els) {
      const st = this.styles[e.style]; if (st?.mode !== 'blocks') continue;
      const m = new T.Mesh(new T.BoxGeometry(e.rect.w, e.h, e.rect.d),
        new T.MeshBasicMaterial({ color: new T.Color(...st.color), transparent: true, opacity: st.opacity, depthWrite: BLOCK_DEPTH_WRITE }));
      m.position.set(e.rect.cx, e.h / 2, e.rect.cz); m.rotation.y = -e.rect.a; m.renderOrder = this.order.wall; this.group.add(m);
    }
  }
}
