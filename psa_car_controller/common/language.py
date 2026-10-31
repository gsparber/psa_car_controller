"""Language of the charts, dates and numbers ("language" in config.ini); the texts of the app stay in English.
AUTO leaves the choice to the browser. The server only keeps the setting, assets/00_language.js applies it."""
import logging

logger = logging.getLogger(__name__)

AUTO = "auto"
LANGUAGES = {
    AUTO: "Browser language",
    "cs-CZ": "Čeština",
    "da-DK": "Dansk",
    "de-AT": "Deutsch (Österreich)",
    "de-CH": "Deutsch (Schweiz)",
    "de-DE": "Deutsch (Deutschland)",
    "el-GR": "Ελληνικά",
    "en-GB": "English (UK)",
    "en-US": "English (US)",
    "es-ES": "Español",
    "fi-FI": "Suomi",
    "fr-BE": "Français (Belgique)",
    "fr-CH": "Français (Suisse)",
    "fr-FR": "Français (France)",
    "hr-HR": "Hrvatski",
    "hu-HU": "Magyar",
    "it-IT": "Italiano",
    "nb-NO": "Norsk",
    "nl-BE": "Nederlands (België)",
    "nl-NL": "Nederlands (Nederland)",
    "pl-PL": "Polski",
    "pt-BR": "Português (Brasil)",
    "pt-PT": "Português (Portugal)",
    "ro-RO": "Română",
    "ru-RU": "Русский",
    "sk-SK": "Slovenčina",
    "sl-SI": "Slovenščina",
    "sv-SE": "Svenska",
    "tr-TR": "Türkçe",
    "uk-UA": "Українська",
}


def normalize(value):
    """The setting as a key of LANGUAGES, for hand written ones too (de, de_at); anything unknown falls back to AUTO."""
    if isinstance(value, str):
        wanted = value.strip().replace("_", "-").lower()
        for key in LANGUAGES:
            if wanted == key.lower():
                return key
        for key in LANGUAGES:
            if wanted and wanted == key.split("-")[0].lower():
                return key
    if value not in (None, ""):
        logger.warning("Unknown language %r, using the one of the browser", value)
    return AUTO


def current():
    """The language picked in config.ini; AUTO when the config can't be read."""
    from psa_car_controller.psacc.application.car_controller import PSACarController  # pylint: disable=C0415
    try:
        return normalize(PSACarController().config.General.language)
    except AttributeError:
        return AUTO
