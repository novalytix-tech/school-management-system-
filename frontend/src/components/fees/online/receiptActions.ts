// ==========================================
// Receipt PDF actions: download / print / view. The PDF is fetched with the
// bearer token (it is not a public URL) and handled as a blob URL.
// Developed by NTS Novalytix
// ==========================================

import { feePortalService } from '../../../services/feePortalService';

export type ReceiptAction = 'download' | 'print' | 'view';

const revokeLater = (url: string) => setTimeout(() => URL.revokeObjectURL(url), 60_000);

/**
 * Run a receipt action. Returns an error message on failure, or null.
 * For "view", a blank window is opened synchronously (before the fetch) so
 * popup blockers treat it as user-initiated.
 */
export async function runReceiptAction(
  receiptId: string,
  action: ReceiptAction,
  receiptNumber?: string | null
): Promise<string | null> {
  const viewWin = action === 'view' ? window.open('', '_blank') : null;
  const res = await feePortalService.getReceiptPdf(receiptId);
  if (!res.success || !res.blob) {
    viewWin?.close();
    return res.error?.message || 'Could not load the receipt.';
  }
  const blob = res.blob.type ? res.blob : new Blob([res.blob], { type: 'application/pdf' });
  const url = URL.createObjectURL(blob);

  if (action === 'download') {
    const a = document.createElement('a');
    a.href = url;
    a.download = res.filename || `${receiptNumber || 'receipt'}.pdf`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    revokeLater(url);
    return null;
  }

  if (action === 'view') {
    if (viewWin) viewWin.location.href = url;
    else window.open(url, '_blank');
    revokeLater(url);
    return null;
  }

  // Print: load the PDF in a hidden iframe and print it from there.
  const iframe = document.createElement('iframe');
  iframe.style.position = 'fixed';
  iframe.style.right = '0';
  iframe.style.bottom = '0';
  iframe.style.width = '0';
  iframe.style.height = '0';
  iframe.style.border = '0';
  iframe.setAttribute('aria-hidden', 'true');
  iframe.title = 'Receipt print frame';
  iframe.onload = () => {
    try {
      iframe.contentWindow?.focus();
      iframe.contentWindow?.print();
    } catch {
      // Some browsers block printing a PDF from a frame: open it instead.
      window.open(url, '_blank');
    }
    setTimeout(() => {
      iframe.remove();
      URL.revokeObjectURL(url);
    }, 60_000);
  };
  iframe.src = url;
  document.body.appendChild(iframe);
  return null;
}
