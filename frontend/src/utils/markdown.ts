import { marked } from 'marked'

const renderer = new marked.Renderer()

// Override code block rendering to include language class
renderer.code = function ({ text, lang }: { text: string; lang?: string }) {
  const langClass = lang ? ` class="language-${lang}"` : ''
  return `<pre><code${langClass}>${escapeHtml(text)}</code></pre>\n`
}

// Override inline code
renderer.codespan = function ({ text }: { text: string }) {
  return `<code>${escapeHtml(text)}</code>`
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

marked.setOptions({
  renderer,
  breaks: true,
  gfm: true,
})

// LaTeX protection: replace math blocks with placeholders before markdown,
// then restore them after. This prevents marked from mangling LaTeX.
let mathBlocks: string[] = []

// Code fences and inline code spans are excluded from math protection: `$`
// inside code is literal, and MathJax skips <pre>/<code> natively, so a
// mangled \(...\) there would be displayed verbatim.
const CODE_RE = /(`{3,})[^\n`]*\n[\s\S]*?\n\1[ \t]*|`{2,}[^`\n]*`{2,}|`[^`\n]*`/g

function protectMathIn(text: string): string {
  // Block math: $$...$$
  text = text.replace(/\$\$([\s\S]*?)\$\$/g, (_: string, math: string) => {
    mathBlocks.push(`\\[${math.trim()}\\]`)
    return `@@MATHBLOCK${mathBlocks.length - 1}@@`
  })
  // Inline math: $...$ (but not $$)
  text = text.replace(/(?<!\$)\$(?!\$)([^\$\n]+?)\$(?!\$)/g, (_: string, math: string) => {
    mathBlocks.push(`\\(${math.trim()}\\)`)
    return `@@MATHBLOCK${mathBlocks.length - 1}@@`
  })
  return text
}

function protectLatex(text: string): string {
  mathBlocks = []
  let out = ''
  let last = 0
  for (const m of text.matchAll(CODE_RE)) {
    out += protectMathIn(text.slice(last, m.index))
    out += m[0] // code region: $ stays literal
    last = m.index + m[0].length
  }
  return out + protectMathIn(text.slice(last))
}

function restoreLatex(html: string): string {
  for (let i = 0; i < mathBlocks.length; i++) {
    html = html.replace(`@@MATHBLOCK${i}@@`, mathBlocks[i])
  }
  return html
}

export function renderMarkdown(text: string): string {
  if (!text) return ''
  const protected_ = protectLatex(text)
  const html = marked.parse(protected_) as string
  return restoreLatex(html)
}
