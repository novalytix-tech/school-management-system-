// ==========================================
// Membership & Tenure History — join / leave / rejoin lifecycle
// For super admins and school admins: see who joined, who left and came back,
// how many times, each stint's period & duration, and total time as a member.
// Developed by NTS Novalytix
// ==========================================

import React, { useState, useMemo, useEffect, useCallback } from 'react';
import { useERP } from '../../context/ERPContext';
import { membershipService, TenureRecord, ACTIVE_SENTINEL_DATE } from '../../services/membershipService';
import { ClassTenureRecord } from '../../types';
import { EmptyState } from '../../components/common/EmptyState';
import { SearchInput } from '../../components/common/SearchInput';
import { formatDate } from '../../utils/formatters';
import {
  Users,
  GraduationCap,
  Briefcase,
  LogIn,
  LogOut,
  RefreshCw,
  ChevronDown,
  ChevronRight,
  History,
  BookOpen,
} from 'lucide-react';

/** Display the open-ended sentinel verbatim (31-12-9999); real dates localised. */
function displayEndDate(iso: string): string {
  if (iso === ACTIVE_SENTINEL_DATE) return '31-12-9999';
  return formatDate(iso);
}

function humanizeDays(days: number): string {
  if (!days || days <= 0) return '0 days';
  if (days < 31) return `${days} day${days === 1 ? '' : 's'}`;
  const months = Math.round(days / 30.44);
  if (months < 12) return `${months} mo`;
  const years = Math.floor(months / 12);
  const rem = months % 12;
  return rem ? `${years}y ${rem}m` : `${years} yr${years === 1 ? '' : 's'}`;
}

export const MembershipHistoryPage: React.FC = () => {
  const { currentUser, currentRole } = useERP();

  const [tenures, setTenures] = useState<TenureRecord[]>(() => membershipService.getTenures());
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState<'' | 'student' | 'teacher'>('');
  const [statusFilter, setStatusFilter] = useState<'' | 'member' | 'left' | 'returnees'>('');
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const reload = useCallback(() => setTenures(membershipService.getTenures()), []);

  useEffect(() => {
    reload();
    const handler = () => reload();
    window.addEventListener('nts_erp_storage_updated', handler);
    return () => window.removeEventListener('nts_erp_storage_updated', handler);
  }, [reload]);

  // School admins see only their institution; super admins see everything.
  const scoped = useMemo(() => {
    if (currentRole === 'super_admin') return tenures;
    const inst = currentUser.institutionId;
    return tenures.filter(t => !t.institutionId || !inst || t.institutionId === inst);
  }, [tenures, currentRole, currentUser.institutionId]);

  const stats = useMemo(() => {
    return {
      total: scoped.length,
      members: scoped.filter(t => t.currentlyMember).length,
      left: scoped.filter(t => !t.currentlyMember).length,
      rejoins: scoped.reduce((s, t) => s + t.rejoinCount, 0),
    };
  }, [scoped]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return scoped.filter(t => {
      const matchType = typeFilter === '' || t.entityType === typeFilter;
      const matchStatus =
        statusFilter === '' ||
        (statusFilter === 'member' && t.currentlyMember) ||
        (statusFilter === 'left' && !t.currentlyMember) ||
        (statusFilter === 'returnees' && t.rejoinCount > 0);
      const matchSearch =
        q === '' ||
        [t.personName, t.personRef, t.roleLabel].some(v => (v ?? '').toString().toLowerCase().includes(q));
      return matchType && matchStatus && matchSearch;
    });
  }, [scoped, search, typeFilter, statusFilter]);

  // Per-teacher class-assignment tenure rows (recomputed whenever history reloads).
  const classTenuresByTeacher = useMemo(() => {
    const map: Record<string, ClassTenureRecord[]> = {};
    membershipService.getClassTenures().forEach(r => {
      (map[r.teacherId] ||= []).push(r);
    });
    return map;
  }, [tenures]);

  const toggle = (id: string) => setExpanded(prev => ({ ...prev, [id]: !prev[id] }));

  const statCard = (label: string, value: number, Icon: React.ElementType, color: string) => (
    <div
      style={{
        flex: 1,
        minWidth: '160px',
        background: '#fff',
        border: '1px solid #e2e8f0',
        borderRadius: '10px',
        padding: '14px 16px',
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
      }}
    >
      <div
        style={{
          width: '40px',
          height: '40px',
          borderRadius: '10px',
          background: `${color}1a`,
          color,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}
      >
        <Icon size={20} />
      </div>
      <div>
        <div style={{ fontSize: '22px', fontWeight: 700, color: '#0f172a', lineHeight: 1.1 }}>{value}</div>
        <div style={{ fontSize: '12px', color: '#64748b' }}>{label}</div>
      </div>
    </div>
  );

  return (
    <div>
      <div className="page-header">
        <div className="page-title-group">
          <h1>Membership & Tenure History</h1>
          <p className="page-subtitle">
            Join, leave and rejoin timeline for every student and staff member — periods, durations, and return counts.
          </p>
        </div>
      </div>

      {/* Summary */}
      <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap', marginBottom: '16px' }}>
        {statCard('People tracked', stats.total, Users, '#4f46e5')}
        {statCard('Currently members', stats.members, LogIn, '#16a34a')}
        {statCard('Left / inactive', stats.left, LogOut, '#dc2626')}
        {statCard('Total rejoins', stats.rejoins, RefreshCw, '#d97706')}
      </div>

      {/* Filters */}
      <div className="filter-toolbar">
        <div className="filter-group-left" style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
          <div style={{ minWidth: '240px' }}>
            <SearchInput value={search} onChange={setSearch} placeholder="Search name, admission / staff id, class" />
          </div>
          <select
            className="form-select filter-select filter-select-md"
            value={typeFilter}
            onChange={e => setTypeFilter(e.target.value as any)}
            aria-label="Filter by type"
          >
            <option value="">All People</option>
            <option value="student">Students</option>
            <option value="teacher">Teachers / Staff</option>
          </select>
          <select
            className="form-select filter-select filter-select-md"
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value as any)}
            aria-label="Filter by status"
          >
            <option value="">Any Status</option>
            <option value="member">Current Members</option>
            <option value="left">Left / Inactive</option>
            <option value="returnees">Rejoined (returnees)</option>
          </select>
        </div>
        <div style={{ color: '#64748b', fontSize: '13px' }}>
          Showing <strong>{filtered.length}</strong> of <strong>{scoped.length}</strong> people
        </div>
      </div>

      {scoped.length === 0 ? (
        <EmptyState
          icon={<History size={40} />}
          title="No membership history yet"
          description="As students and staff join, leave, or rejoin, their tenure timeline will build up here."
        />
      ) : filtered.length === 0 ? (
        <EmptyState title="No people match your filters" description="Try a different search term or filter." />
      ) : (
        <div className="table-responsive">
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ width: '32px' }}></th>
                <th>Name</th>
                <th>Type</th>
                <th>First Joined</th>
                <th style={{ textAlign: 'center' }}>Stints</th>
                <th style={{ textAlign: 'center' }}>Times Left</th>
                <th style={{ textAlign: 'center' }}>Rejoined</th>
                <th>Total Time</th>
                <th>Current Status</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(t => {
                const isOpen = !!expanded[t.entityId];
                return (
                  <React.Fragment key={t.entityId}>
                    <tr style={{ cursor: 'pointer' }} onClick={() => toggle(t.entityId)}>
                      <td>{isOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</td>
                      <td>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          {t.entityType === 'student' ? (
                            <GraduationCap size={15} color="#4f46e5" />
                          ) : (
                            <Briefcase size={15} color="#0891b2" />
                          )}
                          <div>
                            <div style={{ fontWeight: 600, color: '#0f172a' }}>{t.personName}</div>
                            <div style={{ fontSize: '11px', color: '#94a3b8' }}>
                              {t.personRef} {t.roleLabel ? `· ${t.roleLabel}` : ''}
                            </div>
                          </div>
                        </div>
                      </td>
                      <td style={{ textTransform: 'capitalize', color: '#475569' }}>
                        {t.entityType === 'teacher' ? 'Staff' : 'Student'}
                      </td>
                      <td style={{ color: '#475569', whiteSpace: 'nowrap' }}>{formatDate(t.firstJoined)}</td>
                      <td style={{ textAlign: 'center', color: '#475569' }}>{t.stints.length}</td>
                      <td style={{ textAlign: 'center' }}>
                        <span style={{ fontWeight: 600, color: t.leftCount ? '#dc2626' : '#94a3b8' }}>
                          {t.leftCount}
                        </span>
                      </td>
                      <td style={{ textAlign: 'center' }}>
                        <span style={{ fontWeight: 600, color: t.rejoinCount ? '#d97706' : '#94a3b8' }}>
                          {t.rejoinCount}
                        </span>
                      </td>
                      <td style={{ color: '#475569', whiteSpace: 'nowrap' }}>{humanizeDays(t.totalActiveDays)}</td>
                      <td>
                        <span
                          style={{
                            display: 'inline-block',
                            fontSize: '11px',
                            fontWeight: 700,
                            padding: '2px 8px',
                            borderRadius: '999px',
                            color: t.currentlyMember ? '#166534' : '#991b1b',
                            background: t.currentlyMember ? '#dcfce7' : '#fee2e2',
                          }}
                        >
                          {t.currentlyMember ? 'Active member' : 'Left'}
                        </span>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={9} style={{ background: '#f8fafc', padding: '12px 20px' }}>
                          <div style={{ fontSize: '12px', fontWeight: 700, color: '#475569', marginBottom: '8px' }}>
                            MEMBERSHIP PERIODS
                          </div>
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                            {t.stints.map((s, i) => (
                              <div
                                key={i}
                                style={{
                                  display: 'flex',
                                  alignItems: 'center',
                                  gap: '10px',
                                  fontSize: '13px',
                                  color: '#334155',
                                }}
                              >
                                <span
                                  style={{
                                    width: '22px',
                                    height: '22px',
                                    borderRadius: '50%',
                                    background: s.ongoing ? '#16a34a' : '#64748b',
                                    color: '#fff',
                                    display: 'inline-flex',
                                    alignItems: 'center',
                                    justifyContent: 'center',
                                    fontSize: '11px',
                                    fontWeight: 700,
                                    flexShrink: 0,
                                  }}
                                >
                                  {i + 1}
                                </span>
                                <span style={{ fontWeight: 600 }}>{formatDate(s.start)}</span>
                                <span style={{ color: '#94a3b8' }}>→</span>
                                <span style={{ fontWeight: 600 }}>
                                  {s.end ? formatDate(s.end) : 'Present'}
                                </span>
                                <span style={{ color: '#64748b' }}>· {humanizeDays(s.durationDays)}</span>
                                {s.ongoing && (
                                  <span
                                    style={{
                                      fontSize: '10px',
                                      fontWeight: 700,
                                      color: '#166534',
                                      background: '#dcfce7',
                                      padding: '1px 6px',
                                      borderRadius: '999px',
                                    }}
                                  >
                                    ONGOING
                                  </span>
                                )}
                              </div>
                            ))}
                          </div>

                          {/* Class-assignment tenure (teachers only), with the
                              open-ended 31-12-9999 end date while active. */}
                          {t.entityType === 'teacher' && (() => {
                            const classRows = classTenuresByTeacher[t.entityId] || [];
                            return (
                              <div style={{ marginTop: '16px' }}>
                                <div
                                  style={{
                                    fontSize: '12px',
                                    fontWeight: 700,
                                    color: '#475569',
                                    marginBottom: '8px',
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: '6px',
                                  }}
                                >
                                  <BookOpen size={13} /> CLASSES JOINED
                                </div>
                                {classRows.length === 0 ? (
                                  <div style={{ fontSize: '13px', color: '#94a3b8' }}>
                                    No class assignments on record.
                                  </div>
                                ) : (
                                  <div className="table-responsive">
                                    <table
                                      className="data-table"
                                      style={{ background: '#fff', fontSize: '13px' }}
                                    >
                                      <thead>
                                        <tr>
                                          <th>Class</th>
                                          <th>Section</th>
                                          <th>Subject</th>
                                          <th style={{ whiteSpace: 'nowrap' }}>Start Date</th>
                                          <th style={{ whiteSpace: 'nowrap' }}>End Date</th>
                                          <th style={{ textAlign: 'center' }}>Flag</th>
                                        </tr>
                                      </thead>
                                      <tbody>
                                        {classRows.map((c, ci) => (
                                          <tr key={`${c.classId}-${c.subject}-${ci}`}>
                                            <td style={{ fontWeight: 600, color: '#0f172a' }}>
                                              {c.className}
                                            </td>
                                            <td style={{ color: '#475569' }}>{c.section || '—'}</td>
                                            <td style={{ color: '#475569' }}>{c.subject || '—'}</td>
                                            <td style={{ color: '#475569', whiteSpace: 'nowrap' }}>
                                              {formatDate(c.startDate)}
                                            </td>
                                            <td
                                              style={{
                                                whiteSpace: 'nowrap',
                                                fontWeight: c.isActive ? 700 : 500,
                                                color: c.isActive ? '#0891b2' : '#475569',
                                              }}
                                            >
                                              {displayEndDate(c.endDate)}
                                            </td>
                                            <td style={{ textAlign: 'center' }}>
                                              <span
                                                style={{
                                                  display: 'inline-block',
                                                  fontSize: '11px',
                                                  fontWeight: 700,
                                                  padding: '2px 8px',
                                                  borderRadius: '999px',
                                                  color: c.isActive ? '#166534' : '#991b1b',
                                                  background: c.isActive ? '#dcfce7' : '#fee2e2',
                                                }}
                                              >
                                                {c.isActive ? 'Active' : 'Inactive'}
                                              </span>
                                            </td>
                                          </tr>
                                        ))}
                                      </tbody>
                                    </table>
                                  </div>
                                )}
                              </div>
                            );
                          })()}
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

export default MembershipHistoryPage;
