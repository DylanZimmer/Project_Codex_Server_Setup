export interface CountryOption {
  code: string
  label: string
}

export interface LanguageOption {
  code: string
  label: string
}

export interface LanguagesResponse {
  languages: string[]
  countries?: Record<string, CountryOption[]>
}

export interface SearchResultRow {
  source_id: string | null
  source_name: string | null
  name: string
  brand: string | null
  type: string
  country: string
  language: string
  uploaded_at: string | null
}

export interface SearchResponse {
  source_id: string
  source_name: string
  name: string
  brand: string
  type: string
  country: string
  language: string
  uploaded_at: string
}

export interface TranslateResultRow {
  source_id: string | null
  source_name: string | null
  translation: string
  brand: string | null
  type: string
  country: string | null
  language: string
  uploaded_at: string | null
}

export interface TranslateResponse {
  found: boolean
  results: TranslateResultRow[]
}

export interface TranslateRequest {
  term: string
  lang: string
  country: string
}

// Dynamic countries loaded from Neo4j via /languages
export const LANGUAGE_COUNTRY_MAP: Record<string, CountryOption[]> = {}

export const FALLBACK_LANGUAGES = ['en', 'es', 'fr']

// ── Sync with the Codex server ──────────────────────────────────────────────

export interface Assignment {
  country: string
  languages: string[]
}

export interface SyncStatus {
  configured: boolean
  enabled: boolean
  server_url: string | null
  interval_minutes: number
  running: boolean
  assignment: Assignment | null
  server_assignment: Assignment | null
  installed: Record<string, { version: number; installed_at: string }>
  unavailable: string[]
  last_sync_at: string | null
  last_success_at: string | null
  last_error: string | null
  server_message: string | null
  suggestions: Record<'queued' | 'submitted' | 'approved' | 'rejected' | 'failed', number>
  busy?: boolean
}

export type SuggestionStatus = 'queued' | 'submitted' | 'approved' | 'rejected' | 'failed'

export interface Suggestion {
  client_id: string
  canonical: string
  language: string
  country?: string
  type: 'generic' | 'brand'
  text: string
  note?: string
  submitted_by?: string
  status: SuggestionStatus
  created_at: string
  review_note?: string
  error?: string
}

export interface SuggestionInput {
  canonical: string
  language: string
  text: string
  country?: string
  type?: 'generic' | 'brand'
  note?: string
  submitted_by?: string
}

export interface OcrExtractedText {
  rec_text?: string
  rec_score?: number
}

export interface FuzzyResult {
  matching_name?: string
  matching_source?: string
  matching_algorithm?: string
  matching_uid?: number
  matching_row_number?: number
  distance?: number
}

export type OcrResultItem = OcrExtractedText & FuzzyResult
