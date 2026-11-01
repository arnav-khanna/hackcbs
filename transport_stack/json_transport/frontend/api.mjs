import { API_CONFIG } from './config.mjs';

export class ApiError extends Error {
  constructor(message, status = 0, uncertain = false, data = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.uncertain = uncertain;
    this.data = data;
  }
}

export class MobileApi {
  constructor(config = API_CONFIG, fetcher = (...args) => globalThis.fetch(...args), origin = globalThis.location?.origin) {
    this.config = config;
    this.fetcher = fetcher;
    this.base = new URL(config.baseUrl, origin);
    if (this.base.origin !== origin) throw new Error('Use the mobile API origin or a same-origin proxy.');
    if (!this.base.pathname.endsWith('/')) this.base.pathname += '/';
  }

  async request(endpoint, { method = 'GET', body, timeoutMs, signal, query } = {}) {
    const url = new URL(this.config.endpoints[endpoint], this.base);
    if (query) url.search = new URLSearchParams(query).toString();
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, timeoutMs);
    let status = 0;
    try {
      const response = await this.fetcher(url, {
        method, signal: controller.signal, cache: 'no-store',
        ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
      });
      status = response.status;
      if (status === 204) return null;
      const raw = await response.text();
      let data;
      try { data = JSON.parse(raw); }
      catch { throw new ApiError(`Mobile API returned a non-JSON response (HTTP ${status}).`, status, method === 'POST'); }
      if (!response.ok) {
        const detail = typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail || data);
        throw new ApiError(`HTTP ${status}: ${detail}`, status, method === 'POST' && status >= 500, data);
      }
      if (!data || typeof data !== 'object' || Array.isArray(data)) {
        throw new ApiError('Unexpected mobile API response format.', status, method === 'POST');
      }
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (signal?.aborted) throw error;
      const message = controller.signal.aborted ? 'Mobile API request timed out.' : 'Cannot reach the mobile API. Check the endpoint and connection.';
      throw new ApiError(message, status, method === 'POST');
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }

  health(signal) { return this.request('health', { timeoutMs: this.config.healthTimeoutMs, signal }); }
  receive(signal) {
    return this.request('receive', { timeoutMs: this.config.receiveTimeoutMs, signal, query: { timeout_ms: this.config.receiveWaitMs } });
  }
  send(payload, signal) {
    return this.request('send', { method: 'POST', body: payload, timeoutMs: this.config.sendTimeoutMs, signal });
  }
}

export function wait(ms, signal) {
  return new Promise(resolve => {
    if (signal?.aborted) return resolve();
    const done = () => { clearTimeout(timer); signal?.removeEventListener('abort', done); resolve(); };
    const timer = setTimeout(done, ms);
    signal?.addEventListener('abort', done, { once: true });
  });
}
