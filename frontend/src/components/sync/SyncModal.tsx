import React, { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Modal, Button, Alert, Input } from '../ui'
import { getSuggestions, getSyncStatus, runSync, setAssignment } from '../../services/api'
import type { Suggestion, SuggestionStatus, SyncStatus } from '../../types/codex'

interface SyncModalProps {
  fullWidth?: boolean
  /** Called after a sync, so the app can reload its language list. */
  onSyncComplete?: () => void
}

const formatTime = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : '—')

const STATUS_STYLES: Record<SuggestionStatus, string> = {
  queued: 'bg-slate-100 text-slate-700',
  submitted: 'bg-sky-50 text-sky-800',
  approved: 'bg-emerald-50 text-emerald-800',
  rejected: 'bg-rose-50 text-rose-700',
  failed: 'bg-amber-50 text-amber-800',
}

const STATUS_LABELS: Record<SuggestionStatus, string> = {
  queued: 'Waiting to send',
  submitted: 'Waiting for review',
  approved: 'Approved',
  rejected: 'Rejected',
  failed: 'Not accepted',
}

/** Connection to the Codex server: where this box is used, what is installed, and syncing. */
export const SyncModal: React.FC<SyncModalProps> = ({ fullWidth = false, onSyncComplete }) => {
  const { t } = useTranslation()
  const [isOpen, setIsOpen] = useState(false)
  const [status, setStatus] = useState<SyncStatus | null>(null)
  const [suggestions, setSuggestions] = useState<Suggestion[]>([])
  const [country, setCountry] = useState('')
  const [languages, setLanguages] = useState('')
  const [isSyncing, setIsSyncing] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const refresh = useCallback(async () => {
    try {
      const [s, list] = await Promise.all([getSyncStatus(), getSuggestions()])
      setStatus(s)
      setSuggestions(list)
      const current = s.assignment ?? s.server_assignment
      if (current) {
        setCountry(current.country)
        setLanguages(current.languages.join(', '))
      }
    } catch (err) {
      setError((err as Error).message)
    }
  }, [])

  useEffect(() => {
    if (isOpen) {
      setError('')
      setMessage('')
      refresh()
    }
  }, [isOpen, refresh])

  const handleSync = async () => {
    setIsSyncing(true)
    setError('')
    setMessage('')
    try {
      const s = await runSync()
      setStatus(s)
      setSuggestions(await getSuggestions())
      if (s.busy) setMessage('A sync is already running. Check again in a moment.')
      else if (s.last_error) setError(s.last_error)
      else setMessage('Up to date with the Codex server.')
      onSyncComplete?.()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setIsSyncing(false)
    }
  }

  const handleSaveAssignment = async (e: React.FormEvent) => {
    e.preventDefault()
    setIsSaving(true)
    setError('')
    setMessage('')
    try {
      await setAssignment({
        country: country.trim().toUpperCase(),
        languages: languages.split(',').map((l) => l.trim().toLowerCase()).filter(Boolean),
      })
      setMessage(status?.configured ? 'Saved. Syncing the matching packs now…' : 'Saved. Packs will follow once the box is connected.')
      await refresh()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setIsSaving(false)
    }
  }

  const installed = Object.entries(status?.installed ?? {}).sort(([a], [b]) => a.localeCompare(b))

  return (
    <>
      <Button
        variant="nav"
        size="md"
        fullWidth={fullWidth}
        onClick={() => setIsOpen(true)}
        icon={
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
        }
      >
        <span>{t('sync.button') || 'Sync'}</span>
      </Button>

      <Modal
        isOpen={isOpen}
        onClose={() => setIsOpen(false)}
        title={t('sync.title') || 'Codex server'}
        subtitle={t('sync.subtitle') || 'Drug data for where this box is used'}
        icon="🔄"
        maxWidthClass="max-w-2xl"
      >
        <div className="space-y-5 max-h-[70vh] overflow-y-auto pr-1">
          {error && <Alert type="error" message={error} />}
          {message && <Alert type="success" message={message} />}

          {/* Connection */}
          <section className="space-y-2">
            <h4 className="text-xs font-bold uppercase tracking-wide text-slate-500">Connection</h4>
            {status && !status.configured ? (
              <Alert
                type="warning"
                message="Not connected to a Codex server. Set CODEX_SERVER_URL and CODEX_INSTANCE_KEY in .env, then restart the API."
              />
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
                <div className="p-2.5 bg-slate-50 rounded-lg border border-slate-200">
                  <div className="text-slate-500">Server</div>
                  <div className="font-semibold text-slate-800 break-all">{status?.server_url ?? '…'}</div>
                </div>
                <div className="p-2.5 bg-slate-50 rounded-lg border border-slate-200">
                  <div className="text-slate-500">Last successful sync</div>
                  <div className="font-semibold text-slate-800">{formatTime(status?.last_success_at)}</div>
                </div>
                <div className="p-2.5 bg-slate-50 rounded-lg border border-slate-200">
                  <div className="text-slate-500">Checks for updates</div>
                  <div className="font-semibold text-slate-800">every {status?.interval_minutes ?? '…'} min when online</div>
                </div>
              </div>
            )}
            {status?.last_error && !error && <Alert type="warning" message={`Last sync: ${status.last_error}`} />}
            {status?.server_message && <Alert type="info" message={status.server_message} />}
            <Button type="button" variant="primary" size="sm" onClick={handleSync} isLoading={isSyncing} disabled={!status?.configured}>
              {t('sync.now') || 'Sync now'}
            </Button>
          </section>

          {/* Assignment */}
          <section className="space-y-2">
            <h4 className="text-xs font-bold uppercase tracking-wide text-slate-500">Where this box is used</h4>
            <form onSubmit={handleSaveAssignment} className="grid grid-cols-1 sm:grid-cols-[110px_1fr_auto] gap-2 items-end">
              <Input id="sync-country" label="Country" placeholder="UA" value={country} maxLength={2} onChange={(e) => setCountry(e.target.value)} />
              <Input id="sync-languages" label="Languages" placeholder="uk, ru" value={languages} onChange={(e) => setLanguages(e.target.value)} />
              <Button type="submit" variant="secondary" size="md" isLoading={isSaving} className="h-10.5">
                Save
              </Button>
            </form>
            <p className="text-xs text-slate-500">
              Packs for other countries and languages are removed at the next sync.
              {status?.server_assignment && !status.assignment &&
                ` Set on the server: ${status.server_assignment.country} · ${status.server_assignment.languages.join(', ')}.`}
            </p>
          </section>

          {/* Installed packs */}
          <section className="space-y-2">
            <h4 className="text-xs font-bold uppercase tracking-wide text-slate-500">Installed packs</h4>
            {installed.length === 0 ? (
              <p className="text-xs text-slate-500">None yet.</p>
            ) : (
              <ul className="text-xs divide-y divide-slate-100 border border-slate-200 rounded-lg">
                {installed.map(([id, info]) => (
                  <li key={id} className="flex justify-between px-3 py-2">
                    <span className="font-semibold text-slate-800">{id}</span>
                    <span className="text-slate-500">version {info.version} · {formatTime(info.installed_at)}</span>
                  </li>
                ))}
              </ul>
            )}
            {status && status.unavailable.length > 0 && (
              <p className="text-xs text-amber-700">Not available on the server yet: {status.unavailable.join(', ')}</p>
            )}
          </section>

          {/* Suggestions */}
          <section className="space-y-2">
            <h4 className="text-xs font-bold uppercase tracking-wide text-slate-500">Suggestions from this box</h4>
            {suggestions.length === 0 ? (
              <p className="text-xs text-slate-500">None yet. Use “Suggest a better name” under a translation.</p>
            ) : (
              <ul className="text-xs divide-y divide-slate-100 border border-slate-200 rounded-lg">
                {suggestions.map((s) => (
                  <li key={s.client_id} className="px-3 py-2 flex items-start justify-between gap-3">
                    <div>
                      <div className="font-semibold text-slate-800">
                        {s.text} <span className="font-normal text-slate-500">for {s.canonical} ({s.language}{s.country ? ` · ${s.country}` : ''})</span>
                      </div>
                      {(s.review_note || s.error) && <div className="text-slate-500 mt-0.5">{s.review_note || s.error}</div>}
                    </div>
                    <span className={`shrink-0 px-2 py-0.5 rounded-full font-semibold ${STATUS_STYLES[s.status]}`}>
                      {STATUS_LABELS[s.status]}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </Modal>
    </>
  )
}
