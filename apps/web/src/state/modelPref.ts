import type { ModelInfo } from '../api/types'

/**
 * Model / reasoning-effort preference: remember the user's last choice so it
 * carries over across refreshes and new sessions, instead of being reset every
 * time. Only one global preference is stored — "the default for the next new
 * session"; existing sessions each keep their own per-session choice (see
 * App's modelBySession).
 */
const STORAGE_KEY = 'noeta-model-pref'

export interface ModelPref {
  model: string
  effort: string
}

const EMPTY: ModelPref = { model: '', effort: '' }

/** Read the last choice; missing / unparseable falls back to empty. */
export function readModelPref(): ModelPref {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return EMPTY
    const p = JSON.parse(raw) as Partial<ModelPref>
    return {
      model: typeof p.model === 'string' ? p.model : '',
      effort: typeof p.effort === 'string' ? p.effort : '',
    }
  } catch {
    return EMPTY
  }
}

/** Persist the current choice. When localStorage is unavailable (private mode
 * etc.) fail silently — it never affects the main flow. */
export function writeModelPref(pref: ModelPref): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(pref))
  } catch {
    /* Losing the preference is acceptable: next time falls back to the default model. */
  }
}

/**
 * Project the persisted preference onto the currently available models: a model
 * missing from the list falls back to the default model; an effort unsupported
 * by that model falls back to its default_effort. Returns null while models
 * have not loaded yet (empty list) — callers should skip and resolve again
 * after loading (to avoid misjudging the preference as invalid).
 */
export function resolveModelPref(
  models: ModelInfo[],
  defaultModel: string,
): ModelPref | null {
  if (models.length === 0) return null
  const pref = readModelPref()
  const model = models.some((m) => m.id === pref.model)
    ? pref.model
    : defaultModel
  const def = models.find((m) => m.id === model)
  const effort =
    pref.effort && def?.efforts?.includes(pref.effort)
      ? pref.effort
      : (def?.default_effort ?? '')
  return { model, effort }
}
