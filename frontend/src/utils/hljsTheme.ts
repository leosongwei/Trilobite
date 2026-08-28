import { watch } from 'vue'
import darkTheme from 'highlight.js/styles/vs2015.css?inline'
import lightTheme from 'highlight.js/styles/github.css?inline'
import { useTheme } from '../theme'

// highlight.js color themes are plain global CSS, so instead of a static
// import (which would lock the file manager to one palette) we inject the
// matching theme into <head> and swap it whenever the UI theme changes.
const STYLE_ID = 'hljs-theme'

function applyHljsTheme(t: 'dark' | 'beige') {
  let el = document.getElementById(STYLE_ID) as HTMLStyleElement | null
  if (!el) {
    el = document.createElement('style')
    el.id = STYLE_ID
    document.head.appendChild(el)
  }
  // Token colors come from the chosen highlight.js theme; the surface color
  // stays with the app's own tokens, so neutralize the theme's background.
  el.textContent =
    (t === 'beige' ? lightTheme : darkTheme) + '\n.hljs { background: transparent; color: inherit; }'
}

export function initHljsTheme() {
  const { theme } = useTheme()
  watch(theme, applyHljsTheme, { immediate: true })
}
