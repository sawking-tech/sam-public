/* Картинки к реплике: выбор файла, вставка из буфера, перетаскивание; лоток превью под строкой ввода. */

// очередь картинок: {dataUrl: "data:image/png;base64,...", name}
const queue = [];

export const hasImages = () => queue.length > 0;

// забрать очередь целиком: картинки уходят с репликой, лоток пустеет
export function takeImages() {
  const images = queue.slice();
  queue.length = 0;
  renderTray();
  return images;
}

const trayHtml = () => queue.map((it, i) =>
  `<span style="position:relative;display:inline-block;">
       <img class="img-thumb" src="${it.dataUrl}" alt="картинка к сообщению"/>
       <button class="img-del" onclick="_removeImg(${i})" title="Убрать" aria-label="Убрать картинку">×</button>
     </span>`
).join('');

function renderTray() {
  const tray = document.getElementById('img-tray');
  if (!tray) return;
  if (!queue.length) { tray.style.display = 'none'; tray.innerHTML = ''; return; }
  tray.style.display = 'flex';
  tray.innerHTML = trayHtml();
}

// кнопка × на превью
export function removeImage(idx) {
  queue.splice(idx, 1);
  renderTray();
}

function addImageFile(file) {
  if (!file || !file.type || !file.type.startsWith('image/')) return;
  const reader = new FileReader();
  reader.onload = e => {
    queue.push({dataUrl: e.target.result, name: file.name || 'image'});
    renderTray();
  };
  reader.readAsDataURL(file);
}

export function onImagePicked(e) {
  const files = e.target.files || [];
  for (const f of files) addImageFile(f);
  e.target.value = '';  // тот же файл можно выбрать повторно
}

// вставка из буфера обмена и перетаскивание в окно
export function watchImageInput() {
  document.addEventListener('paste', e => {
    const items = (e.clipboardData || {}).items || [];
    let added = 0;
    for (const it of items) {
      if (it.kind === 'file' && it.type.startsWith('image/')) {
        addImageFile(it.getAsFile());
        added++;
      }
    }
    if (added) e.preventDefault();
  });
  ['dragover', 'drop'].forEach(ev => document.addEventListener(ev, e => {
    if (ev === 'dragover') { e.preventDefault(); return; }
    e.preventDefault();
    const files = (e.dataTransfer && e.dataTransfer.files) || [];
    for (const f of files) addImageFile(f);
  }));
}
