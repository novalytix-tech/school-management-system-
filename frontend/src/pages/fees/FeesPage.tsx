// ==========================================
// Fees & Payments Gateway Page
// Developed by NTS Novalytix
// ==========================================

import React from 'react';
import { useERP } from '../../context/ERPContext';
import { ParentFeePortal } from '../../components/fees/ParentFeePortal';
import { AdminFeePortal } from '../../components/fees/AdminFeePortal';
import { ServerFeePortal } from '../../components/fees/online/ServerFeePortal';
import { hasBackendSession } from '../../services/feePortalService';

export const FeesPage: React.FC = () => {
  const { currentRole } = useERP();

  if (currentRole === 'parent' || currentRole === 'student') {
    // Backend session → server-backed portal (server is the source of truth);
    // demo / offline mode keeps the local demo portal.
    return hasBackendSession() ? <ServerFeePortal /> : <ParentFeePortal />;
  }

  return <AdminFeePortal />;
};
