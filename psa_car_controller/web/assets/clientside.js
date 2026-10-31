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

// the same for ui.fmt, which renders <data> with the number as the server writes it
function localizeNumbers (root) {
  Array.from(root.getElementsByClassName('local-number')).forEach(function (element) {
    const value = Number(element.getAttribute('value'))
    const text = numberText(value, Number(element.dataset.digits))
    if (!isNaN(value) && element.textContent !== text) {
      element.textContent = text
    }
  })
}

function localizePage (root) {
  localizeTimes(root)
  localizeNumbers(root)
}

if (typeof document !== 'undefined') {
  // runs before the browser paints, for what the server sends and everything Dash adds or updates later
  new MutationObserver(() => localizePage(document)).observe(document.documentElement, {
    childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['datetime', 'value']
  })
}

// A new choice in the settings: charts take it when they are drawn again, on the next page
function applyLanguage (setting) { // eslint-disable-line no-unused-vars
  window.psaccLanguage.set(setting)
  localizePage(document)
}

// The separators of the number columns, which the table takes in the format of d3
function tableLocale () { // eslint-disable-line no-unused-vars
  const parts = new Intl.NumberFormat(language()).formatToParts(1234567.5)
  const part = type => (parts.find(item => item.type === type) || { value: '' }).value
  return { decimal: part('decimal'), group: part('group') }
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

function updateMap (data, oldFigure, x, y, lastPos) {
  const trips = data.trips
  const figures = []
  let i = 0
  y.forEach(function (yLabel) {
    const xLabel = x[i]
    const figure = Object.assign({}, oldFigure[i])
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
    if (lastPos) {
      if (!figure.layout.map.center) {
        figure.layout.map.zoom = TRIP_ZOOM
      }
      figure.layout.map.center = { lat: lastPos.lat, lon: lastPos.lon }
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
  let avgC02 = new Avg(); let avgKw = new Avg(); const avgTime = new Avg()
  const avgPrice = new Avg()
  data.chargings.forEach(function (charge) {
    const diff = ((new Date(charge.stop_at)) - (new Date(charge.start_at))) / 3600000
    avgKw.addValue(charge.kw)
    avgC02.addValue(charge.co2)
    avgPrice.addValue(charge.price)
    if (diff > 0) {
      avgTime.addValue(diff)
    }
  })
  if (data.chargings.length > 0) {
    avgKw = avgKw.average()
    avgC02 = avgC02.average()
    avgPriceKw = avgPrice.average() / avgKw
    res.avg_emission_kw = avgC02
    res.avg_chg_speed = avgKw / avgTime.average()
  }
  if (data.trips.length > 0) {
    const totalDistance = Math.abs(data.trips[data.trips.length - 1].mileage - data.trips[0].mileage)
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

function filterAndSort (data, range, figures, p, log, sort, config) { // eslint-disable-line no-unused-vars
  if (log > 10) {
    logger.disableLogger()
  }
  const ctx = dash_clientside.callback_context.triggered // eslint-disable-line  no-undef
  const outFigures = []; let dataFiltered
  console.log('figures:', figures)
  console.log('data:', data)
  console.log('ctx', ctx)
  console.log('sort', sort)
  if (ctx.length > 0 && ctx[0].prop_id.endsWith('sort_by')) {
    dataFiltered = filterDataset(data, range)
    // all of them: the other table gets new rows too and would lose its order
    sortMultipleTable(sort, dataFiltered, p.table_src)
    outFigures.push(...updateTables(dataFiltered, p.table_src))
    outFigures.push(...figures.graph)
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
    outFigures.push(...updateMap(dataFiltered, figures.maps, p.map_x_label, p.map_y_label, getLastPosition(dataFiltered.trips)))
    updateCardsValue(longTrips)
  }
  return outFigures
}
