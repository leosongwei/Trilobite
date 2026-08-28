import { ref } from 'vue'

// UI theme: the default dark palette plus a beige (米黄) light alternative.
// The active theme is mirrored onto <html data-theme="..."> so plain CSS can
// pick it up via :root[data-theme='beige'] overrides (see style.css).
export type Theme = 'dark' | 'beige'

const COOKIE_KEY = 'trilobite.theme'
const COOKIE_MAX_AGE = 60 * 60 * 24 * 365 // one year

function readStoredTheme(): Theme {
  const match = document.cookie.match(new RegExp(`(?:^|; )${COOKIE_KEY}=(beige|dark)`))
  if (match) return match[1] as Theme
  // Migration: the preference used to live in localStorage.
  if (localStorage.getItem(COOKIE_KEY) === 'beige') return 'beige'
  return 'dark'
}

const theme = ref<Theme>(readStoredTheme())

function applyToDocument(t: Theme) {
  if (t === 'beige') {
    document.documentElement.dataset.theme = 'beige'
  } else {
    delete document.documentElement.dataset.theme
  }
}

// Apply before any component renders; index.html does the same before the
// bundle loads to avoid a flash of the wrong palette.
applyToDocument(theme.value)

function toggleTheme() {
  theme.value = theme.value === 'beige' ? 'dark' : 'beige'
  applyToDocument(theme.value)
  document.cookie = `${COOKIE_KEY}=${theme.value}; path=/; max-age=${COOKIE_MAX_AGE}; SameSite=Lax`
  localStorage.removeItem(COOKIE_KEY)
}

export function useTheme() {
  return { theme, toggleTheme }
}
