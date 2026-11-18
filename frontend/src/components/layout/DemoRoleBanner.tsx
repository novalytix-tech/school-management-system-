// ==========================================
// Demo Role Switcher Top Banner
// Developed by NTS Novalytix
// ==========================================

import React from 'react';
import { useERP } from '../../context/ERPContext';
import { UserRole } from '../../types';
import { DEMO_USERS } from '../../data/seedData';
import { ShieldCheck, RotateCcw, Download } from 'lucide-react';

// Persona names are derived from DEMO_USERS so the switcher always matches the
// actual logged-in identity for each role (single source of truth).
const ROLES: Array<{ role: UserRole; label: string }> = [
  { role: 'super_admin', label: 'Super Admin' },
  { role: 'school_admin', label: 'School Admin' },
  { role: 'teacher', label: 'Teacher' },
  { role: 'parent', label: 'Parent' },
  { role: 'student', label: 'Student' },
  { role: 'supplier', label: 'Supplier' },
];

const personaName = (role: UserRole): string => DEMO_USERS[role]?.name || role;

export const DemoRoleBanner: React.FC = () => {
  const { currentRole, switchRole, resetToDemoData, exportBackup, addToast } = useERP();

  return (
    <div className="demo-role-banner no-print">
      <div className="demo-role-label">
        <span className="demo-badge">Demo Mode</span>
        <ShieldCheck size={14} color="#3B82F6" />
        <span>Role Persona:</span>
      </div>

      <div className="role-switcher-chips">
        {ROLES.map(r => {
          const isActive = currentRole === r.role;
          return (
            <button
              key={r.role}
              className={`role-chip ${isActive ? 'active' : ''}`}
              onClick={() => {
                switchRole(r.role);
                addToast('info', `Switched to ${r.label}`, `Viewing system from persona of ${personaName(r.role)}`);
              }}
              title={`Switch role to ${r.label} (${personaName(r.role)})`}
            >
              {r.label}
            </button>
          );
        })}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <button
          onClick={exportBackup}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            color: '#71717a',
            fontSize: '11px',
            padding: '4px 10px',
            borderRadius: '8px',
            background: '#ffffff',
            border: '1px solid #E2E8F0',
            cursor: 'pointer',
          }}
          title="Download JSON Backup"
        >
          <Download size={12} />
          <span>Backup</span>
        </button>

        <button
          onClick={() => {
            if (window.confirm('Reset all demo data back to clean factory default state?')) {
              resetToDemoData();
              addToast('success', 'Demo Data Reset', 'All records have been restored to initial sample state.');
            }
          }}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            color: '#a51d29',
            fontSize: '11px',
            padding: '4px 10px',
            borderRadius: '8px',
            background: '#DBEAFE',
            border: '1px solid #fecaca',
            cursor: 'pointer',
          }}
          title="Reset LocalStorage to Original Seed Data"
        >
          <RotateCcw size={12} />
          <span>Reset Data</span>
        </button>
      </div>
    </div>
  );
};

