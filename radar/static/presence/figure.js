// Силуэт человека точками (позы
// не скучные: лёжа - спит на боку, стоя - рука на бедре и вторая у волос, сидя - нога на ногу откинувшись).
// Тело - части (капсулы и шары) по суставам позы; точки сажаются на поверхность частей. Оси частей позы: стоя и сидя [сторона, вверх, вперёд],
// лёжа [вперёд, вверх, вбок] (голова к +вперёд, живот к +вбок); на выходе всегда [вперёд, вверх, вбок].
// Чистый модуль без THREE: проверяется в node. Размеры в метрах, рост и пропорции - таблица BODIES, это данные.

export const BODIES = {            // рост = headY + head
  male: {
    head: 0.105, neck: 0.05, hipY: 0.94, shoulderY: 1.47, headY: 1.715, torsoR: [0.165, 0.15, 0.205], torsoSquash: [0.7, 0.58],   // таз, талия, плечи
    shoulderX: 0.215, arm: [0.047, 0.04, 0.032], leg: [0.085, 0.06, 0.04], hipX: 0.09, upperArm: 0.29, foreArm: 0.27, thigh: 0.42, calf: 0.45, bust: 0,
    sitSpread: 0.2, sitFoot: 0.03,                                                                             // сидя: колени разведены на sitSpread от бёдер, стопы ещё на sitFoot в стороны от колен
  },
  female: {
    head: 0.092, neck: 0.04, hipY: 0.88, shoulderY: 1.35, headY: 1.558, torsoR: [0.15, 0.098, 0.14], torsoSquash: [0.72, 0.66],
    shoulderX: 0.16, arm: [0.035, 0.029, 0.024], leg: [0.07, 0.048, 0.03], hipX: 0.082, upperArm: 0.255, foreArm: 0.235, thigh: 0.4, calf: 0.41, bust: 0.05,
    sitSpread: 0.0, sitFoot: 0.14,                                                                             // девочка сидит коленями вместе, стопы в стороны, ладони на коленях
  },
};
// Рост и вес конкретного человека поверх опорной фигуры своего пола: рост масштабирует высоту, ширина по росту и корню из отношения индексов массы тела.
export const REFERENCE_BODY = { male: { height: 182, weight: 90 }, female: { height: 165, weight: 57 } };
const bmi = (h, w) => w / (h / 100) ** 2;
export function bodyScale(gender, height, weight) {
  const ref = REFERENCE_BODY[gender] || REFERENCE_BODY.male, y = height / ref.height;
  return { y, xz: y * Math.sqrt(bmi(height, weight) / bmi(ref.height, ref.weight)) };
}
export const BODY_GROUPS = ['head', 'torso', 'arm', 'leg'];     // части тела: точка рою летит в ту же часть новой позы (голова в голову, руки в руки), иначе в середине собирается пузырь
export const SEAT_Y = 0.45;            // высота сиденья, куда садится таз
export const LIE_BACK_M = 0.04;        // лёжа на боку: спина и бок касаются поверхности
export const RING_GAP_START_M = 0.04;  // шаг точек по поверхности: растёт, пока фигура не влезет в лимит точек

const add = (p, q) => [p[0] + q[0], p[1] + q[1], p[2] + q[2]];
const sub = (p, q) => [p[0] - q[0], p[1] - q[1], p[2] - q[2]];
const scale = (p, k) => [p[0] * k, p[1] * k, p[2] * k];
const len = p => Math.hypot(p[0], p[1], p[2]);
const norm = p => scale(p, 1 / (len(p) || 1));
const cross = (p, q) => [p[1] * q[2] - p[2] * q[1], p[2] * q[0] - p[0] * q[2], p[0] * q[1] - p[1] * q[0]];
const toward = (a, dir, L) => add(a, scale(norm(dir), L));                     // точка на расстоянии L от a по направлению dir

const limb = (a, b, r0, r1) => ({ a, b, r0, r1 });

// Рука и нога по цели: плечо/бедро -> локоть/колено -> кисть/стопа с заданной длиной сегментов (колено и локоть - готовые точки позы).
function chain(root, mid, end, r) { return [limb(root, mid, r[0], r[1]), limb(mid, end, r[1], r[2])]; }

// Стоячие позы. Оси: [вбок, вверх, вперёд]. Руки задаются
// точками локтя и кисти по сторонам (+1 правая, -1 левая); cool - рука на бедре и вторая у волос, с контрапостом и свободной ногой.
export const STAND_VARIANTS = ['cool', 'down', 'crossed', 'hips', 'behind', 'phone'];
export const STRIDE_VARIANTS = ['stride_l', 'stride_r'];       // шаг: нога вперёд, руки качаются; на остановках идущего человека левая и правая по очереди

function standingArms(B, variant) {
  const sx = B.shoulderX, hx = B.hipX, hy = B.hipY, sy = B.shoulderY, R = B.arm;
  const arm = (s, elbow, hand) => chain([s * sx, sy, 0], elbow, hand, R);
  switch (variant) {
    case 'down':                                                                   // руки опущены вдоль тела
      return [1, -1].map(s => arm(s, [s * (sx + 0.05), sy - 0.27, 0], [s * (sx + 0.07), sy - 0.54, 0.02]));
    case 'crossed':                                                                // руки сложены на груди: кисть лежит на противоположном предплечье
      return [arm(1, [sx - 0.03, sy - 0.24, 0.1], [-0.13, sy - 0.14, 0.14]), arm(-1, [-sx + 0.03, sy - 0.28, 0.13], [0.13, sy - 0.2, 0.17])];
    case 'hips':                                                                   // обе руки на поясе
      return [1, -1].map(s => arm(s, [s * (sx + 0.17), sy - 0.22, 0], [s * (hx + 0.09), hy + 0.08, 0.03]));
    case 'behind':                                                                 // руки за спиной
      return [1, -1].map(s => arm(s, [s * (sx + 0.02), sy - 0.28, -0.06], [s * 0.07, hy + 0.02, -0.14]));
    case 'stride_l': case 'stride_r': {                                            // руки качаются против ног
      const sw = variant === 'stride_l' ? 1 : -1;
      return [1, -1].map(side => arm(side, [side * (sx + 0.04), sy - 0.27, -0.06 * sw * side], [side * (sx + 0.06), sy - 0.53, -0.16 * sw * side]));
    }
    case 'phone':                                                                  // правая держит телефон перед грудью, левая опущена
      return [arm(1, [sx + 0.08, sy - 0.3, 0.1], [0.05, sy - 0.12, 0.28]), arm(-1, [-(sx + 0.05), sy - 0.27, 0], [-(sx + 0.07), sy - 0.54, 0.02])];
    default:                                                                       // cool: правая на бедре, левая к волосам
      return [arm(1, [sx + 0.19, sy - 0.2, 0.0], [hx + 0.08, hy + 0.08, 0.04]), arm(-1, [-sx - 0.17, sy + 0.1, 0.08], [-0.09, B.headY + 0.04, 0.1])];
  }
}

function standingJoints(B, variant = 'cool') {
  const hx = B.hipX, hy = B.hipY, sy = B.shoulderY, cool = !STAND_VARIANTS.slice(1).includes(variant) && !STRIDE_VARIANTS.includes(variant);
  const tilt = cool ? 0.03 : 0;                                                // контрапост: таз вправо, плечи влево
  const J = { pelvis: [tilt, hy, 0], waist: [0, hy + 0.14, 0], chest: [-tilt * 0.7, sy, 0], neck0: [-tilt * 0.7, sy + 0.08, 0], neck1: [-tilt * 0.7, sy + 0.16, 0],
              head: [-tilt * 0.7, B.headY, 0.01], parts: [] };
  const R = { leg: B.leg }, stance = cool ? hx : hx * 1.15;                    // в спокойной позе ноги чуть шире
  const sw = variant === 'stride_l' ? 1 : variant === 'stride_r' ? -1 : 0;                                  // шаг: ведущая нога вперёд, задняя назад на носке
  if (sw) {
    const front = (side) => chain([side * hx, hy, 0], [side * stance, hy - B.thigh * 0.97, 0.13], [side * stance, 0.07, 0.34], R.leg);
    const back = (side) => chain([side * hx, hy, 0], [side * stance, hy - B.thigh * 0.99, -0.06], [side * stance, 0.12, -0.24], R.leg);
    J.parts.push(...standingArms(B, variant).flat(), ...(sw > 0 ? [...front(1), ...back(-1)] : [...front(-1), ...back(1)]));
    return J;
  }
  const legR = chain([hx + tilt, hy, 0], [stance + tilt, hy - B.thigh, 0.0], [stance + tilt, 0.07, 0], R.leg);                  // опорная нога прямая
  const legL = cool ? chain([-hx + tilt, hy, 0], [-hx - 0.13, hy - B.thigh + 0.02, 0.17], [-hx - 0.2, 0.07, 0.06], R.leg)  // свободная нога: колено вперёд и в сторону
                    : chain([-hx, hy, 0], [-stance, hy - B.thigh, 0], [-stance, 0.07, 0], R.leg);
  J.parts.push(...standingArms(B, variant).flat(), ...legR, ...legL);
  return J;
}

function sittingJoints(B) {                                                    // сидит свободно: колени врозь (девочка - вместе), откинувшись, ладони на коленях, голова чуть набок
  const hx = B.hipX, sx = B.shoulderX, drop = B.hipY - SEAT_Y, lean = -0.09, sy = B.shoulderY - drop, hy = SEAT_Y + 0.02, spread = B.sitSpread;
  const J = { pelvis: [0, SEAT_Y, 0], waist: [0, SEAT_Y + 0.14, lean * 0.4], chest: [0, sy, lean], neck0: [0, sy + 0.08, lean], neck1: [0.01, sy + 0.16, lean * 1.05],
              head: [0.03, B.headY - drop, lean * 1.1 + 0.01], parts: [] };
  const leg = s => chain([s * hx, hy, 0], [s * (hx + spread), hy, B.thigh], [s * (hx + spread + B.sitFoot), 0.07, B.thigh + 0.05], B.leg);      // колени в стороны, голени вниз
  const arm = s => chain([s * sx, sy, lean], [s * (sx + 0.1), sy - 0.25, lean + 0.08], [s * (hx + spread * 0.8), hy + 0.12, B.thigh * 0.8], B.arm);   // ладонь на колене
  J.parts.push(...leg(-1), ...leg(1), ...arm(-1), ...arm(1));
  return J;
}

// Спит на боку: оси [вперёд, вверх, вбок], голова к +вперёд,
// живот к +вбок. Спина почти прямая, ноги назад с лёгким сгибом в коленях, обе руки согнуты в локтях, кисти под головой.
function sleepingJoints(B) {
  const U = 0.2;                                                               // высота оси тела над поверхностью (на боку)
  const J = { pelvis: [-0.3, U, 0.0], waist: [-0.12, U, 0.01], chest: [0.08, U + 0.01, 0.02], neck0: [0.23, U, 0.03], neck1: [0.29, U - 0.01, 0.03],
              head: [0.39, U - 0.05, 0.03], parts: [] };
  const upLeg = chain([-0.3, U + 0.06, 0.02], [-0.66, U + 0.07, 0.14], [-1.0, U + 0.03, 0.08], B.leg);        // ноги назад, колено слегка согнуто к животу
  const dnLeg = chain([-0.3, U - 0.06, 0.0], [-0.64, U - 0.06, 0.1], [-0.98, U - 0.08, 0.05], B.leg);
  const upArm = chain([0.1, U + 0.16, 0.02], [0.2, U + 0.12, 0.24], [0.34, U - 0.02, 0.1], B.arm);            // локоть вперёд-в живот, кисть под голову
  const dnArm = chain([0.1, U - 0.12, 0.0], [0.2, U - 0.12, 0.2], [0.35, U - 0.11, 0.07], B.arm);
  J.parts.push(...upLeg, ...dnLeg, ...upArm, ...dnArm);
  return J;
}

// Спит на боку и обнимает: как sleepingJoints, но верхняя рука идёт поверх партнёра, который лежит со стороны живота (+вбок) в HUG_REACH_M.
function hugJoints(B) {
  const J = sleepingJoints(B), U = 0.2;
  const arm = chain([0.1, U + 0.16, 0.02], [0.04, U + 0.2, 0.22], [-0.1, U + 0.1, 0.45], B.arm);                 // локоть над партнёром, кисть на его животе у пупка (талия -0,12 вперёд, партнёр в 0,3 м)
  J.parts.splice(J.parts.length - 4, 2, ...arm);                                                                 // вместо верхней руки sleepingJoints (третья пара с конца)
  return J;
}

// На спине: тело вытянуто вдоль +вперёд, лицом вверх, ноги чуть врозь, руки вдоль тела, голова на подушке. Сечения туловища плоские (squashAxis 1: вверх).
function backJoints(B) {
  const U = 0.115;
  const J = { pelvis: [-0.3, U, 0.0], waist: [-0.12, U, 0.0], chest: [0.08, U + 0.02, 0.0], neck0: [0.23, U + 0.02, 0.0], neck1: [0.29, U + 0.03, 0.0],
              head: [0.4, U + 0.04, 0.0], parts: [], flat: true };
  for (const s of [-1, 1]) {
    J.parts.push(...chain([-0.3, U, s * B.hipX], [-0.68, U, s * (B.hipX + 0.03)], [-1.05, U, s * (B.hipX + 0.07)], B.leg));   // ноги прямые чуть врозь
    J.parts.push(...chain([0.1, U + 0.01, s * B.shoulderX], [-0.06, U - 0.01, s * (B.shoulderX + 0.07)], [-0.24, U - 0.02, s * (B.shoulderX + 0.06)], B.arm));   // руки вдоль тела
  }
  return J;
}

// Части тела позы: капсулы {a, b, r0, r1, squash?} и шары {c, r}; pose: stand, sit, lie.
export const PHALLUS_LENGTH_M = 0.20, PHALLUS_RADIUS_M = 0.02, PHALLUS_FRONT_Z = 0.095, PHALLUS_CROTCH_DROP_M = 0.1;   // 20 см прямо; стоя висит вертикально между ног впереди бёдер, корень в паху, на 10 см ниже тазобедренного сустава   // у мужской фигуры по умолчанию включён (people.<имя>.phallus в плане)

// Часть тела: стоя висит вниз между ног перед бёдрами с плавным изгибом вперёд, сидя лежит вперёд по сиденью с лёгкой дугой.
// Оси [сторона, вверх, вперёд]; ломаная из опорных точек, около 30 см длиной; капсулы по звеньям и головка шаром на конце.
// Висит по гравитации: от паха чуть вперёд, потом дугой вниз, головка заметно ниже корня; редко поднимается (erect).
const PHALLUS_PATH_LENGTH_M = 0.3;     // длина, на которую нарисованы пути ниже
const PHALLUS_PATH = {
  stand: { hang: [[0.80, 0.095], [0.785, 0.11], [0.70, 0.115], [0.60, 0.12], [0.50, 0.12]], erect: [[0.80, 0.095], [0.895, 0.205], [0.99, 0.315]] },   // стояк: почти прямо вперёд-вверх под 45 градусов, 30 см
  sit: { hang: [[0.495, 0.1], [0.50, 0.18], [0.48, 0.24], [0.40, 0.265], [0.31, 0.27]],   // сидя: вперёд к краю сиденья и вниз через его край
          erect: [[0.495, 0.1], [0.59, 0.21], [0.685, 0.32]] },
};   // [вверх, вперёд]
export function phallusParts(pose, state = 'hang') {
  const raw = PHALLUS_PATH[pose === 'sit' ? 'sit' : 'stand'][state === 'erect' ? 'erect' : 'hang'], k = PHALLUS_LENGTH_M / PHALLUS_PATH_LENGTH_M;   // пути заданы на 30 см, длина масштабируется от корня
  const path = raw.map(([y, z]) => [0, raw[0][0] + (y - raw[0][0]) * k, raw[0][1] + (z - raw[0][1]) * k]), r = PHALLUS_RADIUS_M;
  return [...path.slice(1).map((p, i) => ({ a: path[i], b: p, r0: r, r1: r })), { c: path[path.length - 1], r: r * 1.4 }];
}

// Лёжа на боку: оси [вперёд, вверх, вбок], живот к +вбок. Поднятое состояние идёт от паха наружу, к стороне живота, чуть вперёд и вверх; висящее лёжа не рисуется.
// В объятиях (hug, hugR) рисуется тоже, в сторону партнёра (у hugR зеркально, как и вся поза).
const PHALLUS_LIE_ROOT = [-0.33, 0.18, 0.12], PHALLUS_LIE_DIR = [0.2, 0.28, 0.94];
export function phallusLieParts(state) {
  if (state !== 'erect') return [];
  const n = Math.hypot(...PHALLUS_LIE_DIR), r = PHALLUS_RADIUS_M, steps = 4;
  const at = k => PHALLUS_LIE_ROOT.map((v, i) => v + PHALLUS_LIE_DIR[i] / n * PHALLUS_LENGTH_M * (k / steps) + (i === 1 ? 0.02 * Math.sin(Math.PI * k / steps) : 0));      // лёгкий изгиб вверх посередине
  const path = Array.from({ length: steps + 1 }, (_, k) => at(k));
  return [...path.slice(1).map((p, i) => ({ a: path[i], b: p, r0: r, r1: r })), { c: path[steps], r: r * 1.4 }];
}

export function partsOf(gender, pose, variant = 'side', opts = {}) {
  const B = BODIES[gender] || BODIES.male, J = pose === 'sit' ? sittingJoints(B) : pose === 'lie' ? (variant === 'back' ? backJoints(B) : variant === 'hug' || variant === 'hugR' ? hugJoints(B) : sleepingJoints(B)) : standingJoints(B, variant);
  const [rHip, rWaist, rShoulder] = B.torsoR, [sqLow, sqUp] = B.torsoSquash, parts = [];
  parts.push({ c: J.head, r: B.head, group: 'head' });
  parts.push({ a: J.neck0, b: J.neck1, r0: B.neck, r1: B.neck, group: 'head' });
  const flat = J.flat ? { squashAxis: 1 } : {};                                                                      // на спине глубина тела - по вертикали
  parts.push({ a: add(J.pelvis, [0, -0.02, 0]), b: J.waist, r0: rHip, r1: rWaist * 1.05, squash: sqLow, group: 'torso', ...flat });  // таз
  parts.push({ a: J.waist, b: J.chest, r0: rWaist, r1: rShoulder, squash: sqUp, group: 'torso', ...flat });                          // талия - плечи
  if (B.bust) {
    for (const s of [-1, 1]) {                                                                                      // грудь: стоя и сидя вперёд, на боку в живот, на спине вверх
      const c = J.flat ? [J.chest[0] + 0.03, J.chest[1] + 0.09, s * 0.07] : pose === 'lie' ? [J.chest[0] + 0.04, J.chest[1] + s * 0.07, J.chest[2] + 0.1] : [s * 0.07, J.chest[1] - 0.1, J.chest[2] + 0.105];
      parts.push({ c, r: B.bust, group: 'torso' });
    }
  }
  parts.push(...J.parts.map(p => ({ ...p, group: p.r0 <= B.arm[0] + 1e-9 ? 'arm' : 'leg' })));        // руки тоньше ног: по первому радиусу
  if (opts.phallus && gender === 'male' && pose !== 'lie') parts.push(...phallusParts(pose, opts.phallus).map(p => ({ ...p, group: 'torso' })));
  if (opts.phallus && gender === 'male' && pose === 'lie' && ['side', 'hug', 'hugR'].includes(variant)) parts.push(...phallusLieParts(opts.phallus).map(p => ({ ...p, group: 'torso' })));       // лёжа на боку и в объятиях: только поднятое       // часть тела: рисуется и мерцает вместе со всеми точками фигуры
  return pose === 'lie' && variant === 'hugR' ? parts.map(mirrorZ) : parts;            // партнёр с другой стороны живота: зеркало по оси «вбок»
}

const mirrorZ = p => ({ ...p, ...(p.a ? { a: [p.a[0], p.a[1], -p.a[2]], b: [p.b[0], p.b[1], -p.b[2]] } : { c: [p.c[0], p.c[1], -p.c[2]] }) });

function capsulePoints(p, gap) {
  const d = sub(p.b, p.a), L = len(d), axis = norm(d), helper = Math.abs(axis[1]) > 0.9 ? [1, 0, 0] : [0, 1, 0];
  const u = norm(cross(axis, helper)), v = cross(axis, u), rings = Math.max(2, Math.round(L / gap) + 1), out = [];
  for (let k = 0; k < rings; k++) {
    const f = k / (rings - 1), r = p.r0 + (p.r1 - p.r0) * f, m = Math.max(5, Math.round(2 * Math.PI * r / gap)), c = add(p.a, scale(d, f));
    for (let j = 0; j < m; j++) {
      const th = 2 * Math.PI * (j + (k % 2) * 0.5) / m;                         // кольца сдвинуты через одно: ровнее покрытие
      const o = add(scale(u, Math.cos(th) * r), scale(v, Math.sin(th) * r));
      if (p.squash) o[p.squashAxis ?? 2] *= p.squash;
      out.push(add(c, o));
    }
  }
  return out;
}
function spherePoints(p, gap) {
  const n = Math.max(8, Math.round(4 * Math.PI * p.r * p.r / (gap * gap))), out = [];
  for (let i = 0; i < n; i++) {                                                  // спираль Фибоначчи: равномерно по шару
    const y = 1 - 2 * (i + 0.5) / n, rr = Math.sqrt(1 - y * y), th = i * 2.399963;
    out.push([p.c[0] + p.r * rr * Math.cos(th), p.c[1] + p.r * y, p.c[2] + p.r * rr * Math.sin(th)]);
  }
  return out;
}

// Точки фигуры [вперёд, вверх, вбок] не больше limit.
export function figurePoints(gender, pose, limit, variant = 'side', opts = {}) {
  const parts = partsOf(gender, pose, variant, opts);
  let gap = RING_GAP_START_M, pts;
  for (let i = 0; i < 40; i++, gap += 0.005) {
    pts = parts.flatMap(p => (p.c ? spherePoints(p, gap) : capsulePoints(p, gap)).map(q => [...q, BODY_GROUPS.indexOf(p.group)]));       // 4-й элемент точки - часть тела
    if (pts.length <= limit) break;
  }
  const k = opts.height && opts.weight ? bodyScale(gender, opts.height, opts.weight) : { y: 1, xz: 1 }, pivot = pose === 'sit' ? SEAT_Y : 0;     // сидящий масштабируется от сиденья: таз остаётся на нём
  const fit = ([f, y, s, g]) => [f * k.xz, pivot + (y - pivot) * k.y, s * k.xz, g];
  if (pose !== 'lie') return pts.map(([s, y, f, g]) => fit([f, y, s, g]));
  const hug = variant === 'hug' || variant === 'hugR', core = hug ? pts.filter(p => p[3] !== BODY_GROUPS.indexOf('arm')) : pts;      // в объятиях центр - по телу без рук: рука уходит к партнёру, торс остаётся на месте
  const mid = i => (Math.min(...core.map(p => p[i])) + Math.max(...core.map(p => p[i]))) / 2, cf = mid(0), cs = mid(2);   // лежащий центрируется на кровати по длине и ширине: ноги не свисают
  return pts.map(([f, y, s, g]) => fit([f - cf, y + LIE_BACK_M, s - cs, g]));
}
