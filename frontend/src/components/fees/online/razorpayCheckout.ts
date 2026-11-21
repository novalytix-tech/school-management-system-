// ==========================================
// Razorpay checkout.js loader (loaded once, shared) + polling constants.
// Developed by NTS Novalytix
// ==========================================

const RAZORPAY_SDK_URL = 'https://checkout.razorpay.com/v1/checkout.js';
export const POLL_INTERVAL_MS = 3000;
export const POLL_MAX_ATTEMPTS = 40; // ~2 minutes

let sdkPromise: Promise<boolean> | null = null;

/** Load Razorpay checkout.js once (shared across checkouts). */
export function loadRazorpayCheckout(): Promise<boolean> {
  if (typeof window === 'undefined') return Promise.resolve(false);
  if ((window as any).Razorpay) return Promise.resolve(true);
  if (sdkPromise) return sdkPromise;
  sdkPromise = new Promise<boolean>(resolve => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${RAZORPAY_SDK_URL}"]`);
    const script = existing || document.createElement('script');
    script.addEventListener('load', () => resolve(!!(window as any).Razorpay));
    script.addEventListener('error', () => {
      sdkPromise = null; // allow a retry
      resolve(false);
    });
    if (!existing) {
      script.src = RAZORPAY_SDK_URL;
      script.async = true;
      document.body.appendChild(script);
    }
  });
  return sdkPromise;
}
