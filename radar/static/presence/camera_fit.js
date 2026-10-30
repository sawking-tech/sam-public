// Расстояние камеры, при котором весь план попадает в кадр (ракурс «сверху»): считается по проекции плана на экран,
// а не по длине стороны, потому что при почти вертикальном взгляде горизонталь и вертикаль экрана могут идти вдоль любой оси плана.
export const FIT_MARGIN = 1.12;        // запас вокруг плана, доля
export const FIT_BOX_H_M = 2.5;        // высота углов плана: стены до потолка тоже должны поместиться

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const unit = a => { const l = Math.hypot(...a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };

// dir - направление от центра плана к камере; bbox - {minX, minZ, maxX, maxZ}; fovDeg - вертикальный угол; aspect - ширина / высота окна.
export function fitDistance({ dir, bbox, fovDeg, aspect, margin = FIT_MARGIN }) {
  const f = unit([-dir[0], -dir[1], -dir[2]]);                 // куда смотрит камера
  const right = unit(cross(f, [0, 1, 0])), up = cross(right, f);
  const centre = [(bbox.minX + bbox.maxX) / 2, FIT_BOX_H_M / 2, (bbox.minZ + bbox.maxZ) / 2];
  let halfW = 0, halfH = 0;
  for (const x of [bbox.minX, bbox.maxX]) for (const y of [0, FIT_BOX_H_M]) for (const z of [bbox.minZ, bbox.maxZ]) {
    const v = sub([x, y, z], centre);
    halfW = Math.max(halfW, Math.abs(dot(v, right))); halfH = Math.max(halfH, Math.abs(dot(v, up)));
  }
  const tan = Math.tan(fovDeg * Math.PI / 360);
  const depth = Math.max(...[0, FIT_BOX_H_M].flatMap(y => [bbox.minX, bbox.maxX].flatMap(x => [bbox.minZ, bbox.maxZ].map(z => Math.abs(dot(sub([x, y, z], centre), f))))));
  return Math.max(halfH / tan, halfW / (tan * aspect)) * margin + depth;     // + глубина: ближние углы к камере крупнее
}
