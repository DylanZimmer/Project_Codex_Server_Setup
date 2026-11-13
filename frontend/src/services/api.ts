import type {
  Assignment,
  CountryOption,
  LanguagesResponse,
  SearchResponse,
  Suggestion,
  SuggestionInput,
  SyncStatus,
  TranslateResponse,
} from '../types/codex'

// Codex API address. Set VITE_API_BASE_URL at build time when the API is not on localhost:8000.
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

/**
 * Fetches available language codes that have data in Neo4j.
 */
export interface LanguagesData {
  languages: string[]
  countries: Record<string, CountryOption[]>
}

/**
 * Fetches available language codes and their associated countries from Neo4j.
 */
export async function getLanguages(): Promise<LanguagesData> {
  try {
    const response = await fetch(`${API_BASE_URL}/languages`)
    if (!response.ok) {
      throw new Error('Failed to load languages')
    }
    const data = (await response.json()) as LanguagesResponse
    return {
      languages: Array.isArray(data.languages) ? data.languages : [],
      countries: data.countries ?? {},
    }
  } catch (error) {
    console.warn('Failed to fetch languages from Neo4j:', error)
    return {
      languages: [],
      countries: {},
    }
  }
}

/**
 * Searches for a drug / medical term in Neo4j.
 */
export async function searchDrug(query: string): Promise<SearchResponse[]> {
  const response = await fetch(
    `${API_BASE_URL}/search?term=${encodeURIComponent(query.toLowerCase())}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        query: query,
        limit: 20,
      }),
    }
  )

  if (!response.ok) {
    const errorBody = await response.json().catch(() => null)
    throw new Error(errorBody?.detail ?? 'Failed to search')
  }

  const data = await response.json()
  if (Array.isArray(data)) {
    return data as SearchResponse[]
  }
  return data ? [data as SearchResponse] : []
}

/**
 * Translates a drug term to a destination language and country.
 */
export async function translateDrug(
  term: string,
  lang: string,
  country: string
): Promise<TranslateResponse> {
  const response = await fetch(`${API_BASE_URL}/translate`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      term,
      lang,
      country,
    }),
  })

  if (!response.ok) {
    const errorBody = await response.json().catch(() => null)
    throw new Error(errorBody?.detail ?? 'Translation request failed')
  }

  return (await response.json()) as TranslateResponse
}

/**
 * Scans an image file using OCR and extracts medical terms with fuzzy matching.
 */
export async function scanOcrImage(file: File, sourceLanguage?: string): Promise<import('../types/codex').OcrResultItem[]> {
  const formData = new FormData()
  formData.append('file', file)
  if (sourceLanguage && sourceLanguage !== 'all') {
    formData.append('source_language', sourceLanguage)
  }

  const response = await fetch(`${API_BASE_URL}/ocrmatching/`, {
    method: 'POST',
    body: formData,
  })

  if (!response.ok) {
    const errData = await response.json().catch(() => null)
    throw new Error(errData?.detail || 'OCR extraction failed')
  }

  const data = await response.json()
  if (Array.isArray(data)) {
    return data
  } else if (data && Array.isArray(data.results)) {
    return data.results
  }
  return []
}

// ── Sync with the Codex server ──────────────────────────────────────────────

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...(init?.headers ?? {}) },
  })
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(data?.detail ?? `Request failed (${response.status})`)
  }
  return data as T
}

/** Connection to the Codex server, this box's assignment, installed packs, last sync. */
export function getSyncStatus(): Promise<SyncStatus> {
  return requestJson<SyncStatus>('/sync/status')
}

/** Syncs with the Codex server now and returns the status afterwards. */
export function runSync(): Promise<SyncStatus> {
  return requestJson<SyncStatus>('/sync/run', { method: 'POST' })
}

/** Sets where this box is used; the next sync installs and removes packs to match. */
export async function setAssignment(assignment: Assignment): Promise<Assignment> {
  const data = await requestJson<{ assignment: Assignment }>('/sync/assignment', {
    method: 'PUT',
    body: JSON.stringify(assignment),
  })
  return data.assignment
}

/** Queues a better name for review on the server; it is sent at the next sync. */
export async function submitSuggestion(input: SuggestionInput): Promise<Suggestion> {
  const data = await requestJson<{ suggestion: Suggestion }>('/suggestions', {
    method: 'POST',
    body: JSON.stringify(input),
  })
  return data.suggestion
}

export async function getSuggestions(): Promise<Suggestion[]> {
  const data = await requestJson<{ suggestions: Suggestion[] }>('/suggestions')
  return data.suggestions
}
