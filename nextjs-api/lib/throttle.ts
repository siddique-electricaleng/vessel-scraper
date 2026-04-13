/**
 * Per-source rate limiter.
 * Ensures a minimum delay between requests to each source domain,
 * preventing IP bans during bulk scraping.
 */
export class SourceThrottle {
  private minInterval: number;
  private lastRequest = new Map<string, number>();
  private locks = new Map<string, Promise<void>>();

  constructor(minIntervalMs: number = 2000) {
    this.minInterval = minIntervalMs;
  }

  async wait(source: string): Promise<void> {
    // Queue behind any pending wait for this source
    const pending = this.locks.get(source);
    if (pending) {
      await pending;
    }

    const promise = this._doWait(source);
    this.locks.set(source, promise);
    await promise;
    this.locks.delete(source);
  }

  private async _doWait(source: string): Promise<void> {
    const now = Date.now();
    const last = this.lastRequest.get(source) ?? 0;
    const waitFor = this.minInterval - (now - last);
    if (waitFor > 0) {
      await new Promise((r) => setTimeout(r, waitFor));
    }
    this.lastRequest.set(source, Date.now());
  }
}

export const throttle = new SourceThrottle(2000);
