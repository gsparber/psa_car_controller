/* The language of the charts, dates and numbers: the one picked in the settings, else the one of the browser.
   Runs before Dash starts, so the first chart already has it. The texts of the app stay in English. */
window.psaccLanguage = (function () {
  const FALLBACK = 'en-US'
  // plotly has "no" for Norwegian, browsers say "nb" or "nn"
  const PLOTLY_ALIASES = { nb: 'no', nn: 'no' }
  const meta = document.querySelector('meta[name="psacc-language"]')
  const plotlyLocales = meta ? meta.dataset.plotlyLocales.split(' ') : []
  const requested = new Set()
  const api = {}
  let plotly

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

  function loadPlotlyLocale (name) {
    const file = name.toLowerCase()
    if (!plotlyLocales.includes(file) || requested.has(file)) {
      return
    }
    requested.add(file)
    const url = `${meta.dataset.plotlyLocalesUrl}plotly-locale-${file}.js`
    if (document.readyState === 'loading') {
      // waits for the file, which registers itself: a script added any other way could come after the first chart
      document.write(`<script src="${url}"><\/script>`) // eslint-disable-line no-useless-escape
    } else {
      const script = document.createElement('script')
      script.src = url
      document.head.appendChild(script)
    }
  }

  function applyToPlotly () {
    if (plotly && plotly.setPlotConfig) {
      plotly.setPlotConfig({ locale: plotlyLocale(api.tag) })
    }
  }

  api.set = function (setting) {
    api.tag = resolve(setting)
    loadPlotlyLocale(plotlyLocale(api.tag))
    applyToPlotly()
  }
  api.plotlyLocale = plotlyLocale

  // dcc.Graph loads plotly when it first needs it and plots right away: this is the moment to set its default
  Object.defineProperty(window, 'Plotly', {
    configurable: true,
    enumerable: true,
    get: () => plotly,
    set: function (value) {
      plotly = value
      applyToPlotly()
    }
  })

  api.set(meta ? meta.content : 'auto')
  return api
}())
