// Переходы между состояниями человека на странице радара.
// Состояние - {x, z, face, figKey}: где стоит, куда смотрит, какая поза (ключ фигуры). Какие изменения считаются переходом - таблица RULES; что страница делает с переходом - flightFor
// (рой летит только при смене места на ходу, разворот и сдвиг на месте плавные, смена позы сразу). Новое состояние (например, цвет или одежда) - ещё одна строка, а не правка в трёх местах.
// Чистый модуль без THREE и DOM: проверяется в node.
export const MOVE_M = 0.2;             // сдвиг больше шума оценки положения
export const TURN_RAD = 0.35;          // разворот больше этого (около 20 градусов)

const turnBetween = (a, b) => Math.abs(Math.atan2(Math.sin(b - a), Math.cos(b - a)));

export const RULES = [
  { name: 'move', changed: (a, b) => Math.hypot(a.x - b.x, a.z - b.z) > MOVE_M },     // сменил место: идёт шагами по реперным точкам
  { name: 'pose', changed: (a, b) => a.figKey !== b.figKey },                          // сменил позу или вариант: от позы к позе
  { name: 'turn', changed: (a, b) => turnBetween(a.face, b.face) > TURN_RAD },         // развернулся: фигура не крутится твёрдым телом
];

// Имена сработавших правил между прежним и новым состоянием; прежнего нет (первое появление) - переходов нет, человек просто возникает на месте.
export const transitionsBetween = (prev, next) => (prev ? RULES.filter(r => r.changed(prev, next)).map(r => r.name) : []);

// Что делает страница с переходом: рой летит только при смене места на ходу (идёт сейчас или остановился на прошлом кадре - последний шаг до точки);
// разворот и сдвиг позы на месте (стул, кровать) идут плавно, смена позы и варианта (в том числе поднятое состояние) применяется сразу.
export const flightFor = (changes, walking) => changes.includes('move') && walking;
export const SLIDE_RATE = 5;           // 1/с: плавный сдвиг центра при шаге вне ходьбы
export const TURN_RATE = 4;            // 1/с: плавный разворот
export const smoothAngle = (from, to, rate, dt) => from + turnSigned(from, to) * (1 - Math.exp(-rate * dt));
const turnSigned = (a, b) => Math.atan2(Math.sin(b - a), Math.cos(b - a));
export const SLIDE_MAX_M = 1.0;        // плавно скользит только сдвиг до метра (шаг на кровати, стул); дальше - смена места без ходьбы, сразу, не через стены
export const slides = (dx, dz) => Math.hypot(dx, dz) <= SLIDE_MAX_M;
