/* PSACC service worker: app shell offline, last known page content when the server can't be reached.
   Commands are never answered from the cache. */
const CACHE = 'psacc-v1'
const BASE = new URL('./', self.location).pathname
// Only these Dash callbacks may fall back to a cached answer: they render pages, they don't send anything to the car
const CACHEABLE_OUTPUTS = ['page-content.children']

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll([BASE])).catch(() => {}))
  self.skipWaiting()
})

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
    .then(() => self.clients.claim()))
})

function isOk (response) {
  // An auth proxy (Authelia) answers an expired session with a redirect to its login page: don't cache that
  return response && response.ok && !response.redirected && response.type !== 'opaqueredirect'
}

async function notifyClients (offline) {
  const clients = await self.clients.matchAll()
  clients.forEach(client => client.postMessage({ type: 'psacc-offline', offline }))
}

async function networkFirst (request, cacheKey) {
  const cache = await caches.open(CACHE)
  try {
    const response = await fetch(request)
    if (isOk(response)) {
      cache.put(cacheKey || request, response.clone())
    }
    notifyClients(false)
    return response
  } catch (error) {
    notifyClients(true)
    const cached = await cache.match(cacheKey || request, { ignoreVary: true })
    if (cached) {
      return cached
    }
    throw error
  }
}

async function cacheFirst (request) {
  const cache = await caches.open(CACHE)
  const cached = await cache.match(request)
  if (cached) {
    return cached
  }
  const response = await fetch(request)
  if (isOk(response) || response.type === 'opaque') {
    cache.put(request, response.clone())
  }
  return response
}

async function dashCallback (request) {
  const body = await request.clone().text()
  let output = ''
  try {
    output = JSON.parse(body).output
  } catch (e) {}
  if (!CACHEABLE_OUTPUTS.includes(output)) {
    return fetch(request).then(response => {
      notifyClients(false)
      return response
    }, error => {
      notifyClients(true)
      throw error
    })
  }
  // The Cache API only stores GET requests: key the answer on the callback inputs instead
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body))
  const hash = Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('')
  const response = await networkFirst(request, new Request(BASE + '__callback_cache/' + hash))
  return response
}

self.addEventListener('fetch', event => {
  const request = event.request
  const url = new URL(request.url)
  if (url.origin === self.location.origin && url.pathname.startsWith(BASE)) {
    const path = url.pathname.slice(BASE.length)
    if (request.method === 'POST' && path === '_dash-update-component') {
      event.respondWith(dashCallback(request))
    } else if (request.method !== 'GET') {
      // every other POST (forms, API) goes straight to the server
    } else if (request.mode === 'navigate') {
      event.respondWith(networkFirst(request, new Request(BASE)))
    } else if (path.startsWith('_dash-component-suites/') || path.startsWith('assets/') ||
               path.startsWith('vehicle_picture/')) {
      event.respondWith(cacheFirst(request))
    } else if (path === '_dash-layout' || path === '_dash-dependencies' || path === '_favicon.ico') {
      event.respondWith(networkFirst(request))
    }
  } else if (request.method === 'GET' &&
             ['cdn.jsdelivr.net', 'cdn.plot.ly', 'fonts.googleapis.com', 'fonts.gstatic.com'].includes(url.hostname)) {
    event.respondWith(cacheFirst(request))
  }
})
