// Lazy-load MathJax and provide helpers for typesetting dynamic content.

let mathJaxReady: Promise<void> | null = null

function loadMathJax(): Promise<void> {
  if (!mathJaxReady) {
    ;(window as any).MathJax = {
      loader: {
        paths: {
          fonts: '/vendor',
          sre: '/assets/sre',
        },
      },
      tex: {
        inlineMath: [
          ['$', '$'],
          ['\\(', '\\)'],
        ],
        displayMath: [
          ['$$', '$$'],
          ['\\[', '\\]'],
        ],
      },
      options: {
        // Disable accessibility features - we only need formula rendering
        enableEnrichment: false,
        enableSpeech: false,
        enableBraille: false,
        enableExplorer: false,
        enableComplexity: false,
        enableMenu: false,
        sre: {
          speech: 'none',
        },
      },
      startup: {
        typeset: false,
      },
    }
    mathJaxReady = import('mathjax/tex-chtml.js').then(async () => {
      const mj = (window as any).MathJax
      if (mj?.startup?.promise) {
        await mj.startup.promise
      }
    })
  }
  return mathJaxReady
}

function getMJ() {
  return (window as any).MathJax || null
}

// Serialize typeset calls so they never overlap. Each call waits for the
// previous one to finish - no typeset is ever skipped.
let typesetChain: Promise<void> = Promise.resolve()

export function typesetMath(el: HTMLElement): Promise<void> {
  typesetChain = typesetChain.then(async () => {
    if (!el.isConnected) return
    try {
      await loadMathJax()
      const mj = getMJ()
      if (!mj?.startup?.document) return
      // Reset all MathJax state so re-typesetting works after session switches.
      // typesetClear() properly resets internal document state; doc.clear()
      // alone only clears the math-item list without resetting the processing
      // state, causing subsequent typesets to silently fail.
      if (mj.typesetClear) {
        mj.typesetClear()
      } else {
        mj.startup.document.clear()
      }
      await Promise.race([
        mj.typesetPromise([el]),
        new Promise<void>((_, reject) =>
          setTimeout(() => reject(new Error('typesetPromise timeout after 15s')), 15000),
        ),
      ])
    } catch (err) {
      console.warn('[MathJax] typeset error:', err)
    }
  })
  return typesetChain
}
