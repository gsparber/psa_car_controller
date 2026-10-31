/* The language of the charts, dates and numbers: the one picked in the settings, else the one of the browser.
   Inline in the head of the page, see web/app.py: Dash starts after it, so the first chart already has it.
   The texts of the app stay in English. */
window.psaccLanguage = (function () {
  const FALLBACK = 'en-US'
  const LOCALE_WAIT_MS = 3000
  // plotly has "no" for Norwegian, browsers say "nb" or "nn"
  const PLOTLY_ALIASES = { nb: 'no', nn: 'no' }
  const meta = document.querySelector('meta[name="psacc-language"]')
  const plotlyLocales = meta ? meta.dataset.plotlyLocales.split(' ') : []
  const requested = new Set()
  const api = {}

  function resolve (setting) {
    const wanted = setting && setting !== 'auto' ? setting : navigator.language
    try {
      return Intl.DateTimeFormat.supportedLocalesOf(wanted)[0] || FALLBACK
    } catch (e) { // not a language tag
      return FALLBACK
    }
  }

  // The name plotly knows the language by: its file for it (de-CH, else de), English without one
  function plotlyLocale (tag) {
    const code = tag.toLowerCase()
    const base = code.split('-')[0]
    const file = [code, PLOTLY_ALIASES[base] || base].find(name => plotlyLocales.includes(name))
    if (file) {
      return file.replace(/-.*/, region => region.toUpperCase())
    }
    return base === 'en' && code !== 'en-us' ? 'en' : FALLBACK
  }

  // The file registers itself, before or after plotly. The promise is for the start of Dash: it never fails, and it
  // doesn't wait for long, without the file the charts are in English
  function loadPlotlyLocale (name) {
    const file = name.toLowerCase()
    if (!plotlyLocales.includes(file) || requested.has(file)) {
      return Promise.resolve()
    }
    requested.add(file)
    return new Promise(resolve => {
      const script = document.createElement('script')
      script.onload = script.onerror = () => resolve()
      script.src = `${meta.dataset.plotlyLocalesUrl}plotly-locale-${file}.js`
      document.head.appendChild(script)
      setTimeout(resolve, LOCALE_WAIT_MS)
    })
  }

  // For what writes numbers without Intl, tables and plotly: they would have other separators than the rest of the page
  function separators () {
    const parts = new Intl.NumberFormat(api.tag).formatToParts(1234567.5)
    const part = type => (parts.find(item => item.type === type) || { value: '' }).value
    return { decimal: part('decimal'), group: part('group') }
  }

  function applyToPlotly () {
    if (window.Plotly && window.Plotly.setPlotConfig) {
      const { decimal, group } = separators()
      const locale = plotlyLocale(api.tag)
      try {
        // comes before the file of the language, which keeps the rest: the names of months, the format of dates
        window.Plotly.setPlotConfig({ locale, locales: { [locale]: { format: { decimal, thousands: group } } } })
      } catch (error) { // the charts still work, in English
        console.warn('Language of the charts not set', error)
      }
    }
  }

  // dcc.Graph adds the script of plotly when it first needs it and plots when the script has loaded. A listener on the
  // document comes before the one on the script, so plotly gets its default before the first chart. Plotly has no other
  // default than setPlotConfig, and the server can't put the language in the config of a graph: it doesn't know "auto".
  function plotlyLoaded () {
    if (window.Plotly) {
      document.removeEventListener('load', plotlyLoaded, true)
      applyToPlotly()
    }
  }

  api.set = function (setting) {
    api.tag = resolve(setting)
    api.ready = loadPlotlyLocale(plotlyLocale(api.tag))
    applyToPlotly()
  }
  api.plotlyLocale = plotlyLocale
  api.separators = separators

  document.addEventListener('load', plotlyLoaded, true)
  api.set(meta ? meta.content : 'auto')
  return api
}())
