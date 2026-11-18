// ==========================================
// loadingBus — tiny global activity tracker
// Counts in-flight network requests (and manual
// "tasks") so a single global progress bar can
// show whenever anything is loading.
// Developed by NTS Novalytix
// ==========================================

type Listener = (active: number) => void;

let active = 0;
const listeners = new Set<Listener>();

function emit() {
  listeners.forEach(fn => {
    try {
      fn(active);
    } catch {
      /* a broken listener must never break the app */
    }
  });
}

export const loadingBus = {
  /** Mark one activity as started. */
  start() {
    active += 1;
    emit();
  },
  /** Mark one activity as finished. */
  done() {
    active = Math.max(0, active - 1);
    emit();
  },
  /** Current number of in-flight activities. */
  get active() {
    return active;
  },
  /** Wrap a promise so it is counted automatically. */
  async track<T>(p: Promise<T>): Promise<T> {
    this.start();
    try {
      return await p;
    } finally {
      this.done();
    }
  },
  /** Subscribe to activity changes. Returns an unsubscribe function. */
  subscribe(fn: Listener): () => void {
    listeners.add(fn);
    fn(active);
    return () => listeners.delete(fn);
  },
};
