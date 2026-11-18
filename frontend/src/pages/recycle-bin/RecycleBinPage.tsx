// ==========================================
// Recycle Bin — view, restore, or permanently purge deleted records
// Developed by NTS Novalytix
// ==========================================

import React, { useState, useMemo, useEffect, useCallback } from 'react';
import { useERP } from '../../context/ERPContext';
import { recycleBinService } from '../../services/recycleBinService';
import { RecycleBinEntry } from '../../types';
import { ConfirmDialog } from '../../components/common/ConfirmDialog';
import { EmptyState } from '../../components/common/EmptyState';
import { SearchInput } from '../../components/common/SearchInput';
import { formatDate } from '../../utils/formatters';
import { Trash2, RotateCcw, Search as SearchIcon } from 'lucide-react';

export const RecycleBinPage: React.FC = () => {
  const { currentUser, addToast, refreshStore } = useERP();

  const [entries, setEntries] = useState<RecycleBinEntry[]>(() =>
    recycleBinService.getForUser(currentUser)
  );
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('');

  // Confirmation state for destructive actions
  const [purgeTarget, setPurgeTarget] = useState<RecycleBinEntry | null>(null);
  const [confirmEmpty, setConfirmEmpty] = useState(false);

  const reload = useCallback(() => {
    setEntries(recycleBinService.getForUser(currentUser));
  }, [currentUser]);

  // Stay in sync with the store (deletes happening elsewhere, other tabs, etc.)
  useEffect(() => {
    reload();
    const handler = () => reload();
    window.addEventListener('nts_erp_storage_updated', handler);
    return () => window.removeEventListener('nts_erp_storage_updated', handler);
  }, [reload]);

  const entityTypes = useMemo(() => {
    return Array.from(new Set(entries.map(e => e.entityType))).sort();
  }, [entries]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return entries.filter(e => {
      const matchType = typeFilter === '' || e.entityType === typeFilter;
      const matchSearch =
        q === '' ||
        [e.label, e.description, e.entityType, e.source, e.deletedByName].some(v =>
          (v ?? '').toString().toLowerCase().includes(q)
        );
      return matchType && matchSearch;
    });
  }, [entries, search, typeFilter]);

  const handleRestore = (entry: RecycleBinEntry) => {
    const res = recycleBinService.restore(entry.id);
    if (res.success) {
      addToast('success', 'Restored', `"${entry.label}" has been restored to ${entry.source}.`);
      reload();
      refreshStore();
    } else {
      addToast('error', 'Restore failed', res.message || 'Could not restore this item.');
    }
  };

  const handlePurge = (entry: RecycleBinEntry) => {
    recycleBinService.permanentDelete(entry.id);
    addToast('info', 'Permanently deleted', `"${entry.label}" has been removed for good.`);
    reload();
  };

  const handleEmpty = () => {
    const removed = recycleBinService.emptyForUser(currentUser);
    addToast('info', 'Recycle Bin emptied', `${removed} item${removed === 1 ? '' : 's'} permanently deleted.`);
    reload();
  };

  const relativeTime = (iso: string): string => {
    const then = new Date(iso).getTime();
    if (isNaN(then)) return '';
    const diff = Date.now() - then;
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins} min ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs} hr ago`;
    const days = Math.floor(hrs / 24);
    if (days < 30) return `${days} day${days === 1 ? '' : 's'} ago`;
    return formatDate(iso.split('T')[0]);
  };

  return (
    <div>
      <div className="page-header">
        <div className="page-title-group">
          <h1>Recycle Bin</h1>
          <p className="page-subtitle">
            Deleted records are kept here. Restore them, or permanently remove them for good.
          </p>
        </div>
        <div className="page-actions">
          <button
            className="btn btn-danger"
            onClick={() => setConfirmEmpty(true)}
            disabled={entries.length === 0}
          >
            <Trash2 size={16} />
            <span>Empty Recycle Bin</span>
          </button>
        </div>
      </div>

      {/* Filter Bar */}
      <div className="filter-toolbar">
        <div className="filter-group-left" style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
          <div style={{ minWidth: '260px' }}>
            <SearchInput
              value={search}
              onChange={setSearch}
              placeholder="Search by name, type, source, or who deleted it"
            />
          </div>
          <select
            className="form-select filter-select filter-select-md"
            value={typeFilter}
            onChange={e => setTypeFilter(e.target.value)}
            aria-label="Filter by type"
          >
            <option value="">All Types</option>
            {entityTypes.map(t => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
        <div style={{ color: '#64748b', fontSize: '13px' }}>
          Showing <strong>{filtered.length}</strong> of <strong>{entries.length}</strong> deleted items
        </div>
      </div>

      {entries.length === 0 ? (
        <EmptyState
          icon={<Trash2 size={40} />}
          title="Recycle Bin is empty"
          description="Anything you delete across the system will appear here, so you can restore it or remove it permanently."
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={<SearchIcon size={40} />}
          title="No items match your search"
          description="Try a different search term or clear the type filter."
        />
      ) : (
        <div className="table-responsive">
          <table className="data-table">
            <thead>
              <tr>
                <th>Item</th>
                <th>Type</th>
                <th>Deleted From</th>
                <th>Deleted By</th>
                <th>When</th>
                <th style={{ textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(entry => (
                <tr key={entry.id}>
                  <td>
                    <div style={{ fontWeight: 600, color: '#0f172a' }}>{entry.label}</div>
                    {entry.description && (
                      <div style={{ fontSize: '12px', color: '#64748b' }}>{entry.description}</div>
                    )}
                  </td>
                  <td>
                    <span
                      style={{
                        display: 'inline-block',
                        fontSize: '11px',
                        fontWeight: 700,
                        color: '#3730a3',
                        background: '#e0e7ff',
                        padding: '2px 8px',
                        borderRadius: '999px',
                      }}
                    >
                      {entry.entityType}
                    </span>
                  </td>
                  <td style={{ color: '#475569' }}>{entry.source}</td>
                  <td style={{ color: '#475569' }}>
                    {entry.deletedByName}
                    <div style={{ fontSize: '11px', color: '#94a3b8', textTransform: 'capitalize' }}>
                      {entry.deletedByRole?.replace('_', ' ')}
                    </div>
                  </td>
                  <td style={{ color: '#475569', whiteSpace: 'nowrap' }} title={new Date(entry.deletedAt).toLocaleString()}>
                    {relativeTime(entry.deletedAt)}
                  </td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <button
                      className="btn btn-sm btn-secondary"
                      style={{ marginRight: '8px' }}
                      onClick={() => handleRestore(entry)}
                      title="Restore this item"
                    >
                      <RotateCcw size={14} />
                      <span>Restore</span>
                    </button>
                    <button
                      className="btn btn-sm btn-danger"
                      onClick={() => setPurgeTarget(entry)}
                      title="Delete permanently"
                    >
                      <Trash2 size={14} />
                      <span>Delete</span>
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <ConfirmDialog
        isOpen={!!purgeTarget}
        onClose={() => setPurgeTarget(null)}
        onConfirm={() => purgeTarget && handlePurge(purgeTarget)}
        title="Delete permanently?"
        message={`"${purgeTarget?.label}" will be permanently removed and cannot be restored. This action cannot be undone.`}
        confirmLabel="Delete Forever"
        isDestructive
      />

      <ConfirmDialog
        isOpen={confirmEmpty}
        onClose={() => setConfirmEmpty(false)}
        onConfirm={handleEmpty}
        title="Empty Recycle Bin?"
        message={`All ${entries.length} item${entries.length === 1 ? '' : 's'} in your Recycle Bin will be permanently deleted. This cannot be undone.`}
        confirmLabel="Empty Bin"
        isDestructive
      />
    </div>
  );
};

export default RecycleBinPage;
