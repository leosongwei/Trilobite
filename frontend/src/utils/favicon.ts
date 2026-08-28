// Dynamic favicon: the browser tab icon mirrors the sidebar's session status
// dots so a background tab shows Trilobite activity at a glance. The
// aggregation and colors are shared with the sidebar (utils/sessionStatus.ts,
// style.css): running green (#3fb950) > suspended via sleep_until blue
// (#79b8ff) > idle gray. All three are static PNGs; assets live in
// frontend/public/.
import { watchEffect } from 'vue'
import { useStore } from '../store'
import { projectStatus, type SessionStatus } from './sessionStatus'

const FAVICON_HREFS: Record<SessionStatus, string> = {
  running: '/favicon-running.png',
  pending: '/favicon-pending.png',
  idle: '/favicon-idle.png',
}

export function startFaviconSync(): void {
  const { state } = useStore()
  let link = document.querySelector<HTMLLinkElement>('link[rel="icon"]')
  if (!link) {
    link = document.createElement('link')
    link.rel = 'icon'
    document.head.appendChild(link)
  }
  watchEffect(() => {
    const href = FAVICON_HREFS[projectStatus(state.sessions)]
    // Skip no-op writes; href mutation triggers a network fetch in Firefox.
    if (!link.href.endsWith(href)) link.href = href
  })
}
