/* Register the service worker and show a banner while the app can't reach the server. */
(function () {
  function basePath () {
    try {
      const config = JSON.parse(document.getElementById('_dash-config').textContent)
      return config.requests_pathname_prefix || '/'
    } catch (e) {
      return '/'
    }
  }

  function setOffline (offline) {
    document.documentElement.classList.toggle('app-offline', offline)
  }

  window.addEventListener('online', () => setOffline(false))
  window.addEventListener('offline', () => setOffline(true))

  window.addEventListener('load', function () {
    setOffline(!navigator.onLine)
    const banner = document.createElement('div')
    banner.className = 'offline-banner'
    banner.setAttribute('role', 'status')
    banner.textContent = 'Offline · last known status'
    document.body.appendChild(banner)
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.addEventListener('message', function (event) {
        if (event.data && event.data.type === 'psacc-offline') {
          setOffline(event.data.offline)
        }
      })
      const base = basePath()
      navigator.serviceWorker.register(base + 'sw.js', { scope: base }).catch(function (error) {
        console.warn('service worker registration failed', error)
      })
    }
  })
}())
