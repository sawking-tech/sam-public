/* Связь с шлюзом: WebSocket /ws/monitor, разрыв - новая попытка через RECONNECT_MS. */
import {setConn} from './status.js';

const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/monitor`;
const RECONNECT_MS = 2500;

// onEvent - разбор одного события; ошибка в нём не рвёт связь, она уходит в консоль
export function connect(onEvent) {
  const ws = new WebSocket(WS_URL);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(() => connect(onEvent), RECONNECT_MS); };
  ws.onerror = () => ws.close();
  ws.onmessage = ({data}) => { try { onEvent(JSON.parse(data)); } catch (e) { console.error(e); } };
}
