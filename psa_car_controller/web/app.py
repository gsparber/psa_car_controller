import locale
import logging
import sys

import dash_bootstrap_components as dbc
from flask import Flask
from werkzeug import run_simple
from werkzeug.middleware.proxy_fix import ProxyFix

from dash import Dash
try:
    from werkzeug.middleware.dispatcher import DispatcherMiddleware
except ImportError:
    from werkzeug import DispatcherMiddleware

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


class PwaDash(Dash):
    """Dash app that links the web app manifest and icons, so phones can install it to the home screen.
    Links go through get_asset_url so they follow the per request prefix (HA ingress, reverse proxy)."""

    def interpolate_index(self, **kwargs):  # pylint: disable=arguments-differ
        # use-credentials: the manifest must be fetched with cookies when an auth proxy sits in front of the app
        kwargs["metas"] += "\n      " + "\n      ".join([
            f'<link rel="manifest" href="{self.get_asset_url("manifest.webmanifest")}" crossorigin="use-credentials">',
            f'<link rel="apple-touch-icon" href="{self.get_asset_url("images/icon-180.png")}">',
            f'<link rel="icon" type="image/png" sizes="192x192" href="{self.get_asset_url("images/icon-192.png")}">',
            THEME_SCRIPT,
        ])
        return super().interpolate_index(**kwargs)


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
        self.dash_app.config.__dict__["_read_only"] = []

        if prefix == "/":
            self.dash_app.config.requests_pathname_prefix = ""
            self.dash_app.config.url_base_pathname = None
        else:
            environ["HTTP_X_FORWARDED_PREFIX"] = prefix
            if not prefix.endswith("/"):
                prefix += "/"
            self.dash_app.config.requests_pathname_prefix = prefix
            self.dash_app.config.url_base_pathname = prefix
        self.dash_app.config.assets_external_path = prefix
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
    try:
        lang = locale.getlocale()[0].split("_")[0]
        locale.setlocale(locale.LC_TIME, ".".join(locale.getlocale()))  # make sure LC_TIME is set
        if lang != "en":
            locale_url = [f"https://cdn.plot.ly/plotly-locale-{lang}-latest.js"]
        else:
            locale_url = None
    except (IndexError, locale.Error):
        locale_url = None
        logger.warning("Can't get language")
    if unminified:
        locale_url = ["assets/plotly-with-meta.js"]
    app.config["DEBUG"] = debug
    if base_path == "/":
        application = DispatcherMiddleware(app)
        requests_pathname_prefix = "/"
    else:
        application = DispatcherMiddleware(Flask('dummy_app'), {base_path: app})
        requests_pathname_prefix = base_path + "/"
    dash_app = PwaDash(external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP],
                       external_scripts=locale_url, title=title,
                       server=app, requests_pathname_prefix=requests_pathname_prefix,
                       suppress_callback_exceptions=True, meta_tags=META_TAGS)
    dash_app.enable_dev_tools(debug)
    app.wsgi_app = MyProxyFix(dash_app, base_path)
    # keep this line
    importlib.import_module(view)
    return {"hostname": host, "port": port, "application": application, "use_reloader": reloader, "use_debugger": debug}


def run(config):
    return run_simple(**config)
