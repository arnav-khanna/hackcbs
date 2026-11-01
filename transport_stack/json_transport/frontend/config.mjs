// Only the mobile-side Pluto API belongs here. Serve this UI from that API.
// A relative base also supports a same-origin reverse proxy; the API has no CORS.
export const API_CONFIG = Object.freeze({
  baseUrl: '/',
  endpoints: Object.freeze({ health: 'health', send: 'send', receive: 'receive' }),
  healthIntervalMs: 3000,
  healthTimeoutMs: 5000,
  receiveWaitMs: 15000, // Backend permits 0–60000 ms.
  receiveTimeoutMs: 20000,
  sendTimeoutMs: 60000,
  reconnectDelayMs: 2000,
  historyLimit: 100,
});
