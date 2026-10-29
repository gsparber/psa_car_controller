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
    {"name": "theme-color", "content": "#f8f9fa"},
    {"name": "mobile-web-app-capable", "content": "yes"},
    {"name": "apple-mobile-web-app-capable", "content": "yes"},
    {"name": "apple-mobile-web-app-status-bar-style", "content": "default"},
    {"name": "apple-mobile-web-app-title", "content": "PSACC"},
]


class PwaDash(Dash):
    """Dash app that links the web app manifest and icons, so phones can install it to the home screen.
    Links go through get_asset_url so they follow the per request prefix (HA ingress, reverse proxy)."""

    def interpolate_index(self, **kwargs):  # pylint: disable=arguments-differ
        # use-credentials: the manifest must be fetched with cookies when an auth proxy sits in front of the app
        kwargs["metas"] += "\n      " + "\n      ".join([
            f'<link rel="manifest" href="{self.get_asset_url("manifest.webmanifest")}" crossorigin="use-credentials">',
            f'<link rel="apple-touch-icon" href="{self.get_asset_url("images/icon-180.png")}">',
            f'<link rel="icon" type="image/png" sizes="192x192" href="{self.get_asset_url("images/icon-192.png")}">',
        ])
        return super().interpolate_index(**kwargs)


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

        return super().__call__(environ, start_response)


def start_app(*args, **kwargs):
    run(config_flask(*args, **kwargs))


def config_flask(title, base_path, debug: bool, host, port, reloader=False,
                 # pylint: disable=too-many-arguments,too-many-positional-arguments
                 unminified=False, view="psa_car_controller.web.view.views"):
    global app, dash_app
    reload_view = app is not None
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
    if reload_view:
        importlib.reload(view)
    return {"hostname": host, "port": port, "application": application, "use_reloader": reloader, "use_debugger": debug}


def run(config):
    return run_simple(**config)
