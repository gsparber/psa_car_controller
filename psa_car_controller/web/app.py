import logging
import sys
import threading
from contextvars import ContextVar
from functools import lru_cache
from html import escape
from pathlib import Path

import dash_bootstrap_components as dbc
from flask import Flask
from werkzeug import run_simple
from werkzeug.middleware.proxy_fix import ProxyFix

from dash import Dash
from dash._utils import AttributeDict
try:
    from werkzeug.middleware.dispatcher import DispatcherMiddleware
except ImportError:
    from werkzeug import DispatcherMiddleware

from psa_car_controller.common import language
from psa_car_controller.common.mylogger import file_handler
if sys.version_info >= (3, 8):
    import importlib
else:
    import importlib_metadata as importlib

# pylint: disable=invalid-name
app = None
dash_app = None

logger = logging.getLogger(__name__)


META_TAGS = [
    {"name": "viewport", "content": "width=device-width, initial-scale=1, viewport-fit=cover"},
    {"name": "color-scheme", "content": "light dark"},
    {"name": "theme-color", "content": "#f2f2f7", "media": "(prefers-color-scheme: light)"},
    {"name": "theme-color", "content": "#000000", "media": "(prefers-color-scheme: dark)"},
    {"name": "mobile-web-app-capable", "content": "yes"},
    {"name": "apple-mobile-web-app-capable", "content": "yes"},
    {"name": "apple-mobile-web-app-status-bar-style", "content": "default"},
    {"name": "apple-mobile-web-app-title", "content": "PSACC"},
    # Home Assistant answers every request, ingress included, with "Referrer-Policy: no-referrer". The OpenStreetMap
    # tile servers block browser requests without a Referer (403 "Access blocked"), so the map stays empty.
    # A meta referrer overrides the header for this page: tile requests then send the origin, never the path.
    {"name": "referrer", "content": "strict-origin-when-cross-origin"},
]

# Follow the system light/dark setting, also when it changes while the app is open. Inline in <head> so the page
# never flashes in the wrong theme before the assets load. The classic pages under /v1/ were made for light only.
# views calls window.psaccApplyTheme again on every page change, since Dash changes pages without a reload.
THEME_SCRIPT = """<script>(function () {
        var dark = window.matchMedia('(prefers-color-scheme: dark)')
        function apply () {
          var classic = window.location.pathname.indexOf('/v1/') !== -1
          document.documentElement.setAttribute('data-bs-theme', dark.matches && !classic ? 'dark' : 'light')
        }
        window.psaccApplyTheme = apply
        apply()
        dark.addEventListener('change', apply)
      }())</script>"""


# Language files of plotly: in the assets for the prefix and the service worker, but Dash must not link all of them
PLOTLY_LOCALE_DIR = "plotly-locales"
# Inline in <head>, see language_script, not linked with the other assets
LANGUAGE_SCRIPT = Path(__file__).parent / "assets" / "00_language.js"
NOT_LINKED_ASSETS = rf"^(plotly-locale-.*|{LANGUAGE_SCRIPT.stem})\.js$"
# Dash starts once the language file of plotly is there, so the first chart has it and the file holds up nothing else.
# Starts as well when the language script fails: the page then is in English.
RENDERER = """var renderer;
      (function () {
        function start () { renderer = new DashRenderer() }
        Promise.resolve(window.psaccLanguage && window.psaccLanguage.ready).then(start, start)
      }())"""


@lru_cache(maxsize=1)
def plotly_locales():
    files = (Path(__file__).parent / "assets" / PLOTLY_LOCALE_DIR).glob("plotly-locale-*.js")
    return sorted(file.name[len("plotly-locale-"):-len(".js")] for file in files)


@lru_cache(maxsize=1)
def language_script():
    """Inline, so the language file of plotly loads along with the scripts of Dash. As the last of those scripts it
    only started when all the others were there, and Dash had to wait for it: one more round trip on a slow network."""
    return f"<script>{LANGUAGE_SCRIPT.read_text(encoding='utf-8')}</script>"


_request_prefixes = ContextVar("psacc_request_prefixes", default=None)


class RequestPrefixConfig(AttributeDict):
    """Dash config whose path prefixes hold per request: the same app answers Home Assistant ingress and direct calls
    (a sensor polling the API) at the same time, and the threads of the server must not see the prefix of each other.
    Code outside of a request sees the prefix given at start."""

    def set_request_prefixes(self, requests_pathname_prefix, url_base_pathname, assets_external_path):
        _request_prefixes.set({"requests_pathname_prefix": requests_pathname_prefix,
                               "url_base_pathname": url_base_pathname,
                               "assets_external_path": assets_external_path})

    def __getitem__(self, key):
        prefixes = _request_prefixes.get()
        if prefixes is not None and key in prefixes:
            return prefixes[key]
        return super().__getitem__(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class PwaDash(Dash):
    """Dash app that links the web app manifest and icons, so phones can install it to the home screen.
    Links go through get_asset_url so they follow the per request prefix (HA ingress, reverse proxy)."""

    def __init__(self, *args, **kwargs):
        self._setup_lock = threading.Lock()
        super().__init__(*args, **kwargs)
        self.config = RequestPrefixConfig(self.config)
        self.renderer = RENDERER

    def _setup_server(self):
        # Dash marks the setup as done when it starts it: a parallel first request would run on a half-built app
        with self._setup_lock:
            super()._setup_server()

    def interpolate_index(self, **kwargs):  # pylint: disable=arguments-differ
        # use-credentials: the manifest must be fetched with cookies when an auth proxy sits in front of the app
        kwargs["metas"] += "\n      " + "\n      ".join([
            f'<link rel="manifest" href="{self.get_asset_url("manifest.webmanifest")}" crossorigin="use-credentials">',
            f'<link rel="apple-touch-icon" href="{self.get_asset_url("images/icon-180.png")}">',
            f'<link rel="icon" type="image/png" sizes="192x192" href="{self.get_asset_url("images/icon-192.png")}">',
            THEME_SCRIPT,
            self._language_meta(),
            language_script(),
        ])
        return super().interpolate_index(**kwargs)

    def _language_meta(self):
        """For language_script. Read on every request: the setting can change while the app runs."""
        return (f'<meta name="psacc-language" content="{language.current()}"'
                f' data-plotly-locales="{" ".join(plotly_locales())}"'
                f' data-plotly-locales-url="{escape(self.get_asset_url(PLOTLY_LOCALE_DIR + "/"))}">')


LEGACY_PREFIX = "/v1"
LEGACY_PAGES = ("/v1/config", "/v1/stats")


def legacy_page_path(path):
    """The classic pages live one level down (/v1/stats) while Dash loads its scripts and callbacks with urls
    relative to the page (needed behind Home Assistant ingress): those requests arrive as /v1/_dash-...,
    so everything under /v1/ except the pages themselves is served from the root."""
    if path.startswith(LEGACY_PREFIX + "/") and path.rstrip("/") not in LEGACY_PAGES:
        return path[len(LEGACY_PREFIX):]
    return path


class MyProxyFix(ProxyFix):
    def __init__(self, dashapp: Dash, static_prefix):
        self.flask_app = dashapp.server
        self.dash_app = dashapp
        self.static_prefix = static_prefix
        super().__init__(self.flask_app.wsgi_app, x_host=1, x_port=1, x_prefix=1)

    def __call__(self, environ, start_response):
        prefix = environ.get("HTTP_X_INGRESS_PATH") or environ.get("HTTP_X_FORWARDED_PREFIX") or self.static_prefix
        if prefix == "/":
            self.dash_app.config.set_request_prefixes("", None, prefix)
        else:
            environ["HTTP_X_FORWARDED_PREFIX"] = prefix
            if not prefix.endswith("/"):
                prefix += "/"
            self.dash_app.config.set_request_prefixes(prefix, prefix, prefix)
        environ["PATH_INFO"] = legacy_page_path(environ.get("PATH_INFO", ""))
        return super().__call__(environ, start_response)


def start_app(*args, **kwargs):
    run(config_flask(*args, **kwargs))


def config_flask(title, base_path, debug: bool, host, port, reloader=False,
                 # pylint: disable=too-many-arguments,too-many-positional-arguments
                 unminified=False, view="psa_car_controller.web.view.views"):
    global app, dash_app
    # The views bind app and dash_app when imported and register their routes and callbacks there, so a new app would
    # stay empty: no API, nearly no callbacks, and the catch-all of Dash still answers 200. Keep the running one.
    if app is not None:
        raise RuntimeError("config_flask can only be called once per process")
    app = Flask(__name__)
    app.logger.addHandler(file_handler)
    app.config["DEBUG"] = debug
    if base_path == "/":
        application = DispatcherMiddleware(app)
        requests_pathname_prefix = "/"
    else:
        application = DispatcherMiddleware(Flask('dummy_app'), {base_path: app})
        requests_pathname_prefix = base_path + "/"
    dash_app = PwaDash(external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP],
                       external_scripts=["assets/plotly-with-meta.js"] if unminified else None, title=title,
                       server=app, requests_pathname_prefix=requests_pathname_prefix,
                       suppress_callback_exceptions=True, meta_tags=META_TAGS, assets_ignore=NOT_LINKED_ASSETS)
    dash_app.enable_dev_tools(debug)
    app.wsgi_app = MyProxyFix(dash_app, base_path)
    # keep this line
    importlib.import_module(view)
    # threaded: one connection must not stop the others. WebKit opens one in advance and sends nothing for ~30 s,
    # polling waited behind any long request, and a single idle socket stopped the whole server.
    return {"hostname": host, "port": port, "application": application, "use_reloader": reloader, "use_debugger": debug,
            "threaded": True}


def run(config):
    return run_simple(**config)
