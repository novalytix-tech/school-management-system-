// ==========================================
// Confirmation Dialog Component
// Developed by NTS Novalytix
// ==========================================

import React, { useEffect, useRef, useState } from 'react';
import { Modal } from './Modal';
import { AlertTriangle } from 'lucide-react';

interface ConfirmDialogProps {
  isOpen: boolean;
  onClose: () => void;
  /**
   * May return a promise: the confirm button then shows a pending state and is
   * disabled until it settles, so a double-click cannot run the action twice.
   * The dialog closes when the promise resolves and stays open if it rejects.
   */
  onConfirm: () => unknown;
  /** Label shown on the confirm button while an async onConfirm is running. */
  pendingLabel?: string;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  isDestructive?: boolean;
}

export const ConfirmDialog: React.FC<ConfirmDialogProps> = ({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  isDestructive = false,
  pendingLabel = 'Working…',
}) => {
  const [pending, setPending] = useState(false);
  // A ref (not just state) so a second click in the same frame is also ignored.
  const firedRef = useRef(false);

  useEffect(() => {
    // Re-arm for each new opening (pending itself is cleared when the action settles).
    if (isOpen) firedRef.current = false;
  }, [isOpen]);

  const handleConfirm = () => {
    if (firedRef.current) return;
    firedRef.current = true;
    const result = onConfirm();
    if (result && typeof (result as Promise<unknown>).then === 'function') {
      setPending(true);
      (result as Promise<unknown>).then(
        () => {
          setPending(false);
          onClose();
        },
        () => {
          // Failed: re-enable so the user can retry or cancel.
          firedRef.current = false;
          setPending(false);
        }
      );
    } else {
      onClose();
    }
  };

  const guardedClose = () => {
    if (!pending) onClose();
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={guardedClose}
      title={title}
      footer={
        <>
          <button className="btn btn-secondary" onClick={guardedClose} disabled={pending}>
            {cancelLabel}
          </button>
          <button
            className={`btn ${isDestructive ? 'btn-danger' : 'btn-primary'}`}
            onClick={handleConfirm}
            disabled={pending}
            aria-busy={pending || undefined}
          >
            {pending ? pendingLabel : confirmLabel}
          </button>
        </>
      }
    >
      <div style={{ display: 'flex', gap: '16px', alignItems: 'flex-start' }}>
        <div
          style={{
            width: '40px',
            height: '40px',
            borderRadius: '50%',
            background: isDestructive ? '#FEE2E2' : '#FEF3C7',
            color: isDestructive ? '#DC2626' : '#D97706',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          <AlertTriangle size={22} />
        </div>
        <div>
          <p style={{ color: '#475569', fontSize: '14px', lineHeight: '1.5' }}>{message}</p>
        </div>
      </div>
    </Modal>
  );
};

