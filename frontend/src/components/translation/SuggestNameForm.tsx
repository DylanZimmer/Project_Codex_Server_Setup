import React, { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button, Input, Select, Alert } from '../ui'
import { submitSuggestion } from '../../services/api'

interface SuggestNameFormProps {
  canonical: string
  language: string
  country: string
}

/**
 * Lets someone at this box propose a better translated name or a local brand name.
 * It is queued here and sent to the Codex server at the next sync, where a reviewer approves or rejects it.
 */
export const SuggestNameForm: React.FC<SuggestNameFormProps> = ({ canonical, language, country }) => {
  const { t } = useTranslation()
  const [isOpen, setIsOpen] = useState(false)
  const [type, setType] = useState<'generic' | 'brand'>('generic')
  const [text, setText] = useState('')
  const [note, setNote] = useState('')
  const [name, setName] = useState('')
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setDone('')
    if (!text.trim()) {
      setError('Enter the name you suggest.')
      return
    }
    setIsSaving(true)
    try {
      await submitSuggestion({
        canonical,
        language,
        country: country || undefined,
        type,
        text: text.trim(),
        note: note.trim() || undefined,
        submitted_by: name.trim() || undefined,
      })
      setDone('Thank you. Your suggestion will be sent for review at the next sync.')
      setText('')
      setNote('')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setIsSaving(false)
    }
  }

  if (!isOpen) {
    return (
      <button
        type="button"
        onClick={() => setIsOpen(true)}
        className="text-xs font-semibold text-emerald-700 hover:text-emerald-900 underline cursor-pointer"
      >
        {t('suggest.open') || 'Suggest a better name'}
      </button>
    )
  }

  return (
    <form onSubmit={handleSubmit} className="mt-2 p-4 rounded-xl border border-slate-200 bg-white space-y-3">
      <div className="text-xs text-slate-600">
        Suggest a name for <strong>{canonical}</strong> in <strong>{language.toUpperCase()}</strong>
        {country ? <> for <strong>{country.toUpperCase()}</strong></> : null}. A reviewer checks it before it reaches other boxes.
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-[160px_1fr] gap-2 items-end">
        <Select
          id="suggest-type"
          label="What"
          value={type}
          options={[
            { value: 'generic', label: 'Translated name' },
            { value: 'brand', label: 'Local brand name' },
          ]}
          onChange={(e) => setType(e.target.value as 'generic' | 'brand')}
        />
        <Input id="suggest-text" label="Suggested name" value={text} maxLength={300} onChange={(e) => setText(e.target.value)} />
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <Input id="suggest-note" label="Note (optional)" value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} />
        <Input id="suggest-name" label="Your name (optional)" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
      </div>
      {error && <Alert type="error" message={error} />}
      {done && <Alert type="success" message={done} />}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" onClick={() => setIsOpen(false)}>
          Close
        </Button>
        <Button type="submit" variant="primary" size="sm" isLoading={isSaving}>
          Send suggestion
        </Button>
      </div>
    </form>
  )
}
