export type DeckHitlMode = 'disabled' | 'partial' | 'full'

export interface UserSettings {
  validation: {
    requireHitl: boolean
    dcfHitl: boolean
    deckHitlMode: DeckHitlMode
  }
}

export const DEFAULT_USER_SETTINGS: UserSettings = {
  validation: {
    requireHitl: true,
    dcfHitl: true,
    deckHitlMode: 'partial',
  },
}

const KEY = 'agent.userSettings.v1'

export function loadUserSettings(): UserSettings {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return DEFAULT_USER_SETTINGS
    const parsed = JSON.parse(raw) as Partial<UserSettings>
    return {
      validation: {
        requireHitl: parsed.validation?.requireHitl ?? DEFAULT_USER_SETTINGS.validation.requireHitl,
        dcfHitl: parsed.validation?.dcfHitl ?? DEFAULT_USER_SETTINGS.validation.dcfHitl,
        deckHitlMode: parsed.validation?.deckHitlMode ?? DEFAULT_USER_SETTINGS.validation.deckHitlMode,
      },
    }
  } catch {
    return DEFAULT_USER_SETTINGS
  }
}

export function saveUserSettings(settings: UserSettings): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(settings))
  } catch {
    /* localStorage unavailable */
  }
}

