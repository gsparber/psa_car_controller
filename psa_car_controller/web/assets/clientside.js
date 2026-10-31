class Avg {
  constructor () {
    this.total = 0
    this.count = 0
  }

  addValue (value) {
    if (typeof value === 'number') {
      this.count++
      this.total = ((this.total * (this.count - 1)) / this.count) + (value / this.count)
    }
  }

  // NaN without any value: what is calculated with it stays unknown, nbFormat shows that. 0 would be a value
  average () {
    return this.count > 0 ? this.total : NaN
  }

  static getAverageFromKey (array, key) {
    const avg = new Avg()
    array.forEach(function (obj) { avg.addValue(obj[key]) })
    return avg.average()
  }
}
const logger = (function () {
  let oldConsoleLog = null
  const pub = {}

  pub.enableLogger = function enableLogger () {
    if (oldConsoleLog == null) {
      return
    }

    window.console.log = oldConsoleLog
  }

  pub.disableLogger = function disableLogger () {
    oldConsoleLog = console.log
    window.console.log = function () {}
  }

  return pub
}())

// see 00_language.js
function language () {
  return typeof window !== 'undefined' && window.psaccLanguage ? window.psaccLanguage.tag : undefined
}

function pad2 (nb) {
  return String(nb).padStart(2, '0')
}

// The dates of the server are UTC: without an offset, new Date() would take them for the time of the browser
function parseDate (value) {
  if (typeof value === 'string' && !/(Z|[+-]\d\d(:?\d\d)?)$/.test(value)) {
    return new Date(value.replace(' ', 'T') + 'Z')
  }
  return new Date(value)
}

function localDay (date) {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
}

function localHour (date) {
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`
}

const LOCAL_TIME_FORMATS = {
  // en-GB "Fri 30 Oct, 14:35", de-AT "Fr., 30. Okt., 14:35", en-US "Fri, Oct 30, 02:35 PM"
  datetime: date => date.toLocaleString(language(), {
    weekday: 'short', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
  }),
  // only the time today, with the date otherwise
  updated: function (date) {
    if (localDay(date) === localDay(new Date())) {
      return date.toLocaleTimeString(language(), { hour: '2-digit', minute: '2-digit' })
    }
    return date.toLocaleString(language(), { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
  },
  locale: date => `${date.toLocaleTimeString(language())} ${date.toLocaleDateString(language())}`
}

function localTimeText (value, format) {
  const date = parseDate(value)
  return isNaN(date) ? null : (LOCAL_TIME_FORMATS[format] || LOCAL_TIME_FORMATS.datetime)(date)
}

// ui.local_time renders <time> with the time of the server, this puts the time of the browser in it
function localizeTimes (root) {
  Array.from(root.getElementsByClassName('local-time')).forEach(function (element) {
    const text = localTimeText(element.getAttribute('datetime'), element.dataset.format)
    if (text !== null && element.textContent !== text) {
      element.textContent = text
    }
  })
}

function numberText (value, digits) {
  return new Intl.NumberFormat(language(), { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value)
}

function percentText (value, digits) {
  return new Intl.NumberFormat(language(), {
    style: 'percent', minimumFractionDigits: digits, maximumFractionDigits: digits
  }).format(value / 100)
}

// the same for ui.fmt and ui.percent, which render <data> with the number as the server writes it
function localizeNumbers (root) {
  Array.from(root.getElementsByClassName('local-number')).forEach(function (element) {
    const value = Number(element.getAttribute('value'))
    const format = element.dataset.style === 'percent' ? percentText : numberText
    const text = format(value, Number(element.dataset.digits))
    if (!isNaN(value) && element.textContent !== text) {
      element.textContent = text
    }
  })
}

// the same for ui.number_label: {12345} is a number
function localizeLabels (root) {
  Array.from(root.querySelectorAll('[data-label]')).forEach(function (element) {
    const text = element.dataset.label.replace(/\{(-?\d+)\}/g, (_, value) => numberText(Number(value), 0))
    if (element.getAttribute('aria-label') !== text) {
      element.setAttribute('aria-label', text)
    }
  })
}

function localizePage (root) {
  localizeTimes(root)
  localizeNumbers(root)
  localizeLabels(root)
}

// dcc.Link has no aria props: the tab of the page tells screen readers too, not only by its color
function markCurrentTab (root) {
  root.querySelectorAll('.top-nav-link, .bottom-nav-link').forEach(function (link) {
    if (link.classList.contains('active')) {
      link.setAttribute('aria-current', 'page')
    } else {
      link.removeAttribute('aria-current')
    }
  })
}

if (typeof document !== 'undefined') {
  // runs before the browser paints, for what the server sends and everything Dash adds or updates later
  new MutationObserver(() => {
    localizePage(document)
    markCurrentTab(document)
  }).observe(document.documentElement, {
    childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['datetime', 'value', 'data-label']
  })
}

// Plotly knows no CSS variables. The colors of the dark theme of 10_mobile.css: --app-card, --bs-body-color, --app-separator
const DARK_CHART = { background: '#1c1c1e', text: '#dee2e6', line: '#48484a' }
// The template of a chart until the theme asked for another one, which a change back to light needs
const chartTemplates = new WeakMap()

function darkTheme () {
  return document.documentElement.getAttribute('data-bs-theme') === 'dark'
}

// What the figure has in its template stays: colors of the traces, margins of the axes. A layout can't be changed for
// the dark theme, the server doesn't know the theme, so what the figure sets itself wins (bar_chart: no background).
function darkChartTemplate (template) {
  const { data = {}, layout = {} } = template || {}
  const { background, text, line } = DARK_CHART
  const axis = old => ({ ...old, gridcolor: line, linecolor: line, zerolinecolor: line })
  return {
    ...template,
    data: { ...data, bar: (data.bar || [{}]).map(bar => ({ ...bar, marker: { ...bar.marker, line: { color: background, width: 0.5 } } })) },
    layout: {
      ...layout,
      paper_bgcolor: background,
      plot_bgcolor: background,
      font: { ...layout.font, color: text },
      xaxis: axis(layout.xaxis),
      yaxis: axis(layout.yaxis)
    }
  }
}

function themedFigure (figure) {
  if (!darkTheme() || !figure || !figure.layout) {
    return figure
  }
  return { ...figure, layout: { ...figure.layout, template: darkChartTemplate(figure.layout.template) } }
}

// dcc.Graph gives every figure to Plotly.react: the charts of the server, the popups, the ones of the callbacks. Without
// this they all would need the theme as an input, and would show the wrong one until their callback has run.
function themeChartsOfPlotly () {
  if (!window.Plotly || !window.Plotly.react || window.Plotly.react.themed) {
    return
  }
  const react = window.Plotly.react
  window.Plotly.react = function (element, figure, ...others) {
    if (figure && figure.layout) {
      chartTemplates.set(element, figure.layout.template)
    }
    return react.call(this, element, themedFigure(figure), ...others)
  }
  window.Plotly.react.themed = true
}

// The system changed its theme. Only the template: the charts keep their zoom, the map doesn't load new tiles.
function themeCharts () {
  document.querySelectorAll('.js-plotly-plot').forEach(function (element) {
    if (chartTemplates.has(element)) {
      const template = chartTemplates.get(element)
      window.Plotly.relayout(element, { template: darkTheme() ? darkChartTemplate(template) : template })
    }
  })
}

if (typeof document !== 'undefined') {
  // dcc.Graph adds the script of plotly when it first needs it, see plotlyLoaded of 00_language.js
  document.addEventListener('load', themeChartsOfPlotly, true)
  themeChartsOfPlotly()
  // after the listener of THEME_SCRIPT (app.py), the one in <head>
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', themeCharts)
}

// A new choice in the settings: charts take it when they are drawn again, on the next page
function applyLanguage (setting) { // eslint-disable-line no-unused-vars
  if (window.psaccLanguage) {
    window.psaccLanguage.set(setting)
  }
  localizePage(document)
}

// What the percent sign adds before and after the number: ['', ' %'], ['%', '']
function percentAffixes () {
  const parts = new Intl.NumberFormat(language(), { style: 'percent' }).formatToParts(0.8)
  const number = parts.findIndex(item => item.type === 'integer')
  const text = list => list.map(item => item.value).join('')
  return [text(parts.slice(0, number)), text(parts.slice(number + 1))]
}

// The separators of the number columns, which the table takes in the format of d3, and the symbol of the level columns
function tableLocale () { // eslint-disable-line no-unused-vars
  return { ...(window.psaccLanguage && window.psaccLanguage.separators()), symbol: percentAffixes() }
}

function addLocaleDate (data, dateKey) {
  const dateOption = [language(), { hour: 'numeric', minute: 'numeric' }]
  function dateToLocale (row, key) {
    if (row[key] === null || row[key] === undefined) { // a charge in progress has no stop yet
      row[key + '_str'] = ''
      row[key + '_local'] = null
      return
    }
    const date = parseDate(row[key])
    row[key] = date.getTime()
    row[key + '_str'] = date.toLocaleDateString(...dateOption)
    // a number or a date with an offset would be shown as UTC by plotly, it keeps a date without offset as it is
    row[key + '_local'] = `${localDay(date)} ${localHour(date)}:${pad2(date.getSeconds())}`
  }
  let datasetName, dataset
  for ([datasetName, dataset] of Object.entries(data)) {
    dataset.forEach(function (row) {
      dateKey[datasetName].forEach(key => dateToLocale(row, key))
    })
  }
}

// The labels of the date slider, which the server writes in its own time zone. Same steps as get_marks_from_start_end
function localSliderMarks (marks) { // eslint-disable-line no-unused-vars
  if (!marks) {
    return marks
  }
  const stamps = Object.keys(marks).map(Number)
  const step = stamps.length > 1 ? stamps[1] - stamps[0] : 86400
  const res = {}
  stamps.forEach(function (stamp) {
    const date = new Date(stamp * 1000)
    let label = date.toLocaleDateString(language(), { year: '2-digit', month: 'numeric', day: 'numeric' })
    if (step < 86400) {
      label += ` ${pad2(date.getHours())}h${step > 3600 ? '' : pad2(date.getMinutes())}`
    }
    res[stamp] = Object.assign({}, marks[stamp], { label })
  })
  return res
}

// insights_data.series again, with the days and months of the browser
function bucketSeries (spec) {
  const start = new Date(spec.start * 1000)
  const end = new Date(spec.end * 1000)
  const isDay = spec.unit === 'day'
  const key = date => isDay ? localDay(date) : localDay(new Date(date.getFullYear(), date.getMonth(), 1))
  const sums = new Map()
  const last = key(end)
  // from the calendar date and not by adding 24 h: a day with a clock change is not 24 h long
  let current = new Date(start.getFullYear(), start.getMonth(), isDay ? start.getDate() : 1)
  while (key(current) <= last) {
    sums.set(key(current), [0, 0])
    current = isDay
      ? new Date(current.getFullYear(), current.getMonth(), current.getDate() + 1)
      : new Date(current.getFullYear(), current.getMonth() + 1, 1)
  }
  spec.rows.forEach(function ([stamp, value, divisor]) {
    const bucket = key(new Date(stamp * 1000))
    const sum = sums.get(bucket) || [0, 0]
    sum[0] += value
    sum[1] += divisor || 0
    sums.set(bucket, sum)
  })
  const y = Array.from(sums.values()).map(function ([value, divisor]) {
    if (!spec.per_100) {
      return value
    }
    return divisor ? value / divisor * 100 : null
  })
  return { x: Array.from(sums.keys()), y }
}

function localBars (spec, figure) { // eslint-disable-line no-unused-vars
  return Object.assign({}, figure, { data: [Object.assign({}, figure.data[0], bucketSeries(spec))] })
}

function filterDataset (data, range) {
  function dateFromISO (st) {
    return new Date(st).getTime() / 1000
  }
  function isInRange (st) {
    const tsDate = dateFromISO(st)
    return tsDate >= range[0] && tsDate <= range[1]
  }
  const res = {}
  res.trips = data.trips.filter(line => isInRange(line.start_at))
  res.chargings = data.chargings.filter(line => isInRange(line.start_at))
  console.log('filtered_dataset', res)
  return res
}

function filterShortTrip (data, minimumLength) {
  const longTrips = {
    // minimumLength is in km, distance can be in miles (then distance_km keeps the km)
    trips: data.trips.filter(line => ('distance_km' in line ? line.distance_km : line.distance) > minimumLength),
    chargings: data.chargings
  }
  console.log('long trips:', longTrips)
  return longTrips
}

function updateFigures (data, oldFigure, x, y) {
  const trips = data.trips
  const figures = []
  let i = 0
  y.forEach(function (yLabel) {
    const xLabel = x[i]
    const figure = Object.assign({}, oldFigure[i])
    i++
    const xValues = trips.map(a => a[xLabel])
    // for each y label
    for (let j = 0; j < yLabel.length; j++) {
      figure.data[j].y = trips.map(a => a[yLabel[j]])
      figure.data[j].x = xValues
    }
    console.log(xLabel, figure)
    figures.push(figure)
  })
  return figures
}

const TRIP_ZOOM = 12
const MAP_FILL = 0.8 // of the map, the trips keep a margin
const MAP_HEIGHT = 0.9 // of the window, see the style of trips_map
const MAP_TILE = /\/\d+\/\d+\/\d+\.png(\?|$)/
const EMPTY_TILE = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII='
const TILE_RESULTS = 32
const MAP_VIEW = ['center', 'zoom', 'bearing', 'pitch']
const MAP_ERROR = 'Map error.'
const MAP_STOPPED = 'The map stopped working: trips and position on it may be out of date. Reload the page to try again.'
const tileResults = []
let mapStopped = false

function tileProblem (url, status) {
  const host = new URL(url).hostname
  const osm = host.endsWith('openstreetmap.org')
  const referer = 'OpenStreetMap refuses requests without a Referer header, which a proxy, a browser setting or an ' +
    'extension can remove.'
  if (status === 0) {
    return `${host} could not be reached: offline, blocked by a firewall, or refused. ${osm ? referer : ''}`.trim()
  }
  if (status === 200) {
    return `${host} answered with something that is not a tile.`
  }
  return `${host} answered with HTTP ${status}. ${osm && status === 403 ? referer : ''}`.trim()
}

// null without a failure among the last tiles: the hint leaves when the tiles are back
function tilesHint (results) {
  const failed = results.filter(result => result.problem)
  if (failed.length === 0) {
    return null
  }
  const start = failed.length === results.length ? 'No map background' : 'Parts of the map background are missing'
  return `${start}, the trips and the position are up to date. ${failed[failed.length - 1].problem} ` +
    'Reload the page to try again.'
}

function showMapHint () {
  const element = document.getElementById('map-tiles-hint')
  if (element) {
    const text = mapStopped ? MAP_STOPPED : tilesHint(tileResults)
    element.hidden = !text
    element.textContent = text
  }
}

function noteTile (problem) {
  tileResults.push({ problem })
  tileResults.splice(0, tileResults.length - TILE_RESULTS)
  showMapHint()
}

// What fetchTile doesn't get: an image that can't be decoded, the style, or the map no more loading tiles with fetch.
// Plotly then stops drawing for good, see fetchTile, and the only sign of it is this rejection of its promise.
function noteMapError (event) {
  if (event.reason && event.reason.message === MAP_ERROR) {
    mapStopped = true
    showMapHint()
  }
}

if (window.addEventListener) {
  window.addEventListener('unhandledrejection', noteMapError)
}

// Plotly fails a drawing on any error of the map, one missing tile is enough. The failed promise stays in gd._promises
// and fails every later drawing too: trips, marker and hint stop following the slider. So the map gets an empty tile
// and the page tells what is missing. An image the browser can't decode still is an error of the map.
function fetchTile (load) {
  return async function (input, init) {
    const url = String(input.url || input)
    if (!MAP_TILE.test(url)) {
      return load(input, init)
    }
    let problem
    try {
      const response = await load(input, init)
      if (response.ok && (response.headers.get('Content-Type') || '').startsWith('image/')) {
        noteTile(null)
        return response
      }
      problem = tileProblem(url, response.status)
    } catch (error) {
      if (error.name === 'AbortError') { // the map moved on, it doesn't need this tile any more
        throw error
      }
      problem = tileProblem(url, 0)
    }
    noteTile(problem)
    const bytes = Uint8Array.from(atob(EMPTY_TILE), char => char.charCodeAt(0))
    return new Response(bytes, { headers: { 'Content-Type': 'image/png' } })
  }
}

if (window.fetch) {
  window.fetch = fetchTile(window.fetch.bind(window))
}

// Center and zoom that show all the points, plotly has no fitBounds for a map. The map is 512 pixels wide at zoom 0.
function fitMap (points, width, height) {
  const mercator = lat => Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360))
  const lats = points.map(point => mercator(point.lat))
  const lons = points.map(point => point.lon)
  const [south, north] = [Math.min(...lats), Math.max(...lats)]
  const [west, east] = [Math.min(...lons), Math.max(...lons)]
  const zoomFor = (pixels, part) => Math.log2(pixels * MAP_FILL / 512 / part)
  return {
    center: {
      lat: (2 * Math.atan(Math.exp((south + north) / 2)) - Math.PI / 2) * 180 / Math.PI,
      lon: (west + east) / 2
    },
    zoom: Math.max(0, Math.min(TRIP_ZOOM, zoomFor(width, (east - west) / 360), zoomFor(height, (north - south) / (2 * Math.PI))))
  }
}

// relayoutData has a view when the user moved the map. It has to be kept right away, a resize replaces it.
function userMapView (relayoutData) { // eslint-disable-line no-unused-vars
  if (!relayoutData || relayoutData['map.zoom'] === undefined) {
    return dash_clientside.no_update // eslint-disable-line  no-undef
  }
  const view = {}
  MAP_VIEW.forEach(key => { view[key] = relayoutData[`map.${key}`] })
  return view
}

function updateMap (data, oldFigure, x, y, lastPos, userViews = []) {
  const trips = data.trips
  const figures = []
  let i = 0
  y.forEach(function (yLabel) {
    const xLabel = x[i]
    const figure = Object.assign({}, oldFigure[i])
    const userView = userViews[i]
    i++
    figure.data[0].lat = []
    figure.data[0].lon = []
    figure.data[0].hovertext = []
    for (const trip of trips) {
      const xPos = trip.positions[xLabel]
      figure.data[0].lat.push(...xPos, null)
      figure.data[0].lon.push(...trip.positions[yLabel[0]], null)
      figure.data[0].hovertext.push(...Array(xPos.length).fill(trip[yLabel[1]]), null)
    }
    // null and not empty: plotly drops the map when no trace has an entry
    figure.data[1].lat = [lastPos ? lastPos.lat : null]
    figure.data[1].lon = [lastPos ? lastPos.lon : null]
    if (userView) {
      Object.assign(figure.layout.map, userView)
    } else if (lastPos) { // nothing to show keeps the view too: the map would jump around the world with the slider
      const points = []
      figure.data[0].lat.forEach((lat, index) => {
        const lon = figure.data[0].lon[index]
        if (typeof lat === 'number' && typeof lon === 'number') {
          points.push({ lat, lon })
        }
      })
      Object.assign(figure.layout.map, fitMap(points, window.innerWidth || 800, (window.innerHeight || 600) * MAP_HEIGHT))
    }
    // a layout from before the hint, kept by the service worker, has none
    const hints = figure.layout.annotations || []
    hints.forEach(hint => { hint.visible = !lastPos })
    console.log(xLabel, figure)
    figures.push(figure)
  })
  return figures
}

function removeObjectFromTableRow (data) {
  const rows = []
  data.forEach(object => {
    const row = {}
    for (const key in object) {
      if (typeof object[key] !== 'object') {
        row[key] = object[key]
      }
    }
    rows.push(row)
  })
  return rows
}

function updateTables (data, tables) {
  console.log('tables', tables)
  const figures = []
  tables.forEach(function (table) {
    const tableData = removeObjectFromTableRow(data[table.src])
    figures.push(tableData)
  })
  return figures
}

function nbFormat (nb) {
  if (!Number.isFinite(nb)) {
    return '–'
  }
  return numberText(nb, Math.abs(nb) >= 1000 ? 0 : 2)
}

// The least a charging station offers is 6 A at 230 V, 1.38 kW. Slower, and the dates of a charge can't be the time it
// charged: nothing saw it end in time. Same value as MIN_CHARGE_KW of insights_data.py.
const MIN_CHARGE_KW = 1

function chargingSpeed (chargings) {
  let energy = 0; let time = 0
  chargings.forEach(function (charge) {
    const hours = ((new Date(charge.stop_at)) - (new Date(charge.start_at))) / 3600000
    if (charge.stop_at && hours > 0 && typeof charge.kw === 'number' && charge.kw / hours >= MIN_CHARGE_KW) {
      energy += charge.kw
      time += hours
    }
  })
  return time > 0 ? energy / time : NaN
}

function updateCardsValue (data) {
  // all of them: a range without data must not leave the values of the previous range in the cards
  const res = {
    avg_emission_kw: NaN,
    avg_chg_speed: NaN,
    avg_consum_kw: NaN,
    elec_consum_kw: NaN,
    avg_emission_km: NaN,
    elec_consum_price: NaN,
    avg_consum_price: NaN
  }
  let avgPriceKw
  let avgC02 = new Avg(); let avgKw = new Avg()
  const avgPrice = new Avg()
  data.chargings.forEach(function (charge) {
    avgKw.addValue(charge.kw)
    avgC02.addValue(charge.co2)
    avgPrice.addValue(charge.price)
  })
  if (data.chargings.length > 0) {
    avgKw = avgKw.average()
    avgC02 = avgC02.average()
    avgPriceKw = avgPrice.average() / avgKw
    res.avg_emission_kw = avgC02
    res.avg_chg_speed = chargingSpeed(data.chargings)
  }
  if (data.trips.length > 0) {
    // the rows can come in the order of the table
    const mileages = data.trips.map(trip => trip.mileage)
    const totalDistance = Math.max(...mileages) - Math.min(...mileages)
    res.avg_consum_kw = Avg.getAverageFromKey(data.trips, 'consumption_km')
    res.elec_consum_kw = totalDistance * res.avg_consum_kw / 100
  }
  if (data.trips.length > 0 && data.chargings.length > 0) {
    res.avg_emission_km = res.avg_emission_kw * res.avg_consum_kw / 100
    res.elec_consum_price = avgPriceKw * res.elec_consum_kw
    res.avg_consum_price = avgPriceKw * res.avg_consum_kw
  }
  for (const [key, value] of Object.entries(res)) {
    document.getElementById(key).innerHTML = nbFormat(value)
  }
}

const lastSort = {}

// A table goes from descending to unsorted and only then to ascending, and unsorted looks like ascending for the dates
// and ids, so that click seems to do nothing. This turns unsorted into ascending: a click always changes the order.
function skipUnsorted (tableId, sortBy) { // eslint-disable-line no-unused-vars
  if (sortBy && sortBy.length > 0) {
    lastSort[tableId] = sortBy[0].column_id
    return dash_clientside.no_update // eslint-disable-line  no-undef
  }
  if (lastSort[tableId] === undefined) {
    return dash_clientside.no_update // eslint-disable-line  no-undef
  }
  return [{ column_id: lastSort[tableId], direction: 'asc' }]
}

function sortDataset (ctx, data, tables) {
  const tableId = ctx.prop_id.split('.')[0]
  const table = tables.filter(table => table.table_id === tableId)[0]
  if (ctx.value.length > 0 && data[table.src].length > 0) {
    const asc = ctx.value[0].direction === 'asc'
    let columnId = ctx.value[0].column_id
    let sorted
    if (columnId.endsWith('_str')) {
      columnId = columnId.slice(0, -4)
      sorted = data[table.src].sort(function (a, b) {
        return a[columnId] - b[columnId]
      })
    } else if (data[table.src].some(row => typeof row[columnId] === 'number')) {
      // no price or co2 yet is null, whatever the first row has
      sorted = data[table.src].sort(function (a, b) {
        return a[columnId] - b[columnId]
      })
    } else {
      sorted = data[table.src].sort((a, b) => String(a[columnId] ?? '').localeCompare(String(b[columnId] ?? '')))
    }
    if (asc === false) {
      sorted = sorted.reverse()
    }
    data[table.src] = sorted
  }
}

function sortMultipleTable (sortParams, data, tables) {
  for (const [propId, ctxValue] of Object.entries(sortParams)) {
    const ctx = {}
    ctx.prop_id = propId + '.sort_by'
    ctx.value = ctxValue
    console.log(ctx)
    sortDataset(ctx, data, tables)
  }
}

// The last point with a position, the car can report none. null without any: the map must not make one up
function getLastPosition (trips) {
  const newestFirst = [...trips].sort((a, b) => b.start_at - a.start_at)
  for (const trip of newestFirst) {
    const { lat, long } = trip.positions
    for (let i = lat.length - 1; i >= 0; i--) {
      if (typeof lat[i] === 'number' && typeof long[i] === 'number') {
        return { lat: lat[i], lon: long[i] }
      }
    }
  }
  return null
}

function noUpdates (count) {
  return Array(count).fill(dash_clientside.no_update) // eslint-disable-line  no-undef
}

// mapVisible: a drawn map loads its tiles even in a hidden tab, and they tell the tile server where the trips are.
// So it gets its figure when its tab is open, with what the slider says by then.
function filterAndSort (data, range, figures, p, log, sort, config, userViews = [], mapVisible = true) { // eslint-disable-line no-unused-vars
  if (log > 10) {
    logger.disableLogger()
  }
  const ctx = dash_clientside.callback_context.triggered // eslint-disable-line  no-undef
  const outFigures = []; let dataFiltered
  const others = p.table_src.length + figures.graph.length
  console.log('figures:', figures)
  console.log('data:', data)
  console.log('ctx', ctx)
  console.log('sort', sort)
  if (ctx.length > 0 && ctx.every(trigger => trigger.prop_id.endsWith('.active_tab'))) {
    if (!mapVisible) {
      return noUpdates(others + figures.maps.length)
    }
    addLocaleDate(data, p.date_columns)
    dataFiltered = filterDataset(data, range)
    return [...noUpdates(others),
      ...updateMap(dataFiltered, figures.maps, p.map_x_label, p.map_y_label, getLastPosition(dataFiltered.trips), userViews)]
  }
  if (ctx.length > 0 && ctx[0].prop_id.endsWith('sort_by')) {
    dataFiltered = filterDataset(data, range)
    // all of them: the other table gets new rows too and would lose its order
    sortMultipleTable(sort, dataFiltered, p.table_src)
    outFigures.push(...updateTables(dataFiltered, p.table_src))
    outFigures.push(...figures.graph)
    figures.maps.forEach((figure, index) => Object.assign(figure.layout.map, userViews[index]))
    outFigures.push(...figures.maps)
  } else {
    addLocaleDate(data, p.date_columns)
    dataFiltered = filterDataset(data, range)
    sortMultipleTable(sort, dataFiltered, p.table_src)
    outFigures.push(...updateTables(dataFiltered, p.table_src))
    const longTrips = filterShortTrip(dataFiltered, config.minimumLength)
    console.log('trips', dataFiltered.trips)
    console.log('longTrips', longTrips.trips.length)
    outFigures.push(...updateFigures(longTrips, figures.graph, p.graph_x_label, p.graph_y_label))
    outFigures.push(...updateMap(dataFiltered, figures.maps, p.map_x_label, p.map_y_label, getLastPosition(dataFiltered.trips), userViews))
    updateCardsValue(longTrips)
  }
  return mapVisible ? outFigures : [...outFigures.slice(0, others), ...noUpdates(figures.maps.length)]
}
