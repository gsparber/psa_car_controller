"""Countries the login accepts for each brand.

ApkParser needs res/raw/cultures.json and the parameters.json of the first language of the country in the APK of
the brand. These lists are the countries where both exist, read from github.com/flobz/psa_apk on 2026-10-30. IL is
in cultures.json but has no parameters.json, it can't be used. A new APK can change this: ApkParser stays the
reference and refuses a country its APK doesn't have, tests/test_login_form.py compares with a local APK.
"""
import re

_PEUGEOT = "AR AT AU BE BG BR CH CL CO CY CZ DE DK DZ EE ES FI FR GB GF GP GR HR HU IE IS IT JP KR LT LU LV MA MQ MT " \
           "MX MY NL NO NZ PE PH PL PT RE RO RU SE SG SI SK TH TN TR TW UA UY ZA"
_OPEL = "AT BE BG CH CZ DE DK EE ES FI FR GF GP GR HR HU IE IS IT JP LT LU LV MQ MT NL NO NZ PL PT RE RO RU SE SG SI " \
        "SK TR TW"
_CITROEN = "AR AT AU BE BG BN BR CH CL CO CR CY CZ DE DK DZ EC EE ES FI FR GB GF GP GR HR HU IE IN IR IS IT JP KR LB " \
           "LT LU LV MA MQ MT MY NC NL NO NZ PE PL PT PY RE RO RS RU SE SG SI SK TN TR TW UA UY ZA"
_DS = "AR AT BE BG BN CH CL CO CZ DE DK ES FI FR GB GF GP GR HR HU IE IT JP KR LU MA MQ MY NC NL NO PL PT RE RO RU " \
      "SE SG SI SK TN TR"
_VAUXHALL = "GB"

BRAND_COUNTRIES = {"com.psa.mym.mypeugeot": frozenset(_PEUGEOT.split()),
                   "com.psa.mym.myopel": frozenset(_OPEL.split()),
                   "com.psa.mym.mycitroen": frozenset(_CITROEN.split()),
                   "com.psa.mym.myds": frozenset(_DS.split()),
                   "com.psa.mym.myvauxhall": frozenset(_VAUXHALL.split())}

COUNTRY_NAMES = {"AR": "Argentina", "AT": "Austria", "AU": "Australia", "BE": "Belgium", "BG": "Bulgaria",
                 "BN": "Brunei", "BR": "Brazil", "CH": "Switzerland", "CL": "Chile", "CO": "Colombia",
                 "CR": "Costa Rica", "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany", "DK": "Denmark",
                 "DZ": "Algeria", "EC": "Ecuador", "EE": "Estonia", "ES": "Spain", "FI": "Finland", "FR": "France",
                 "GB": "United Kingdom", "GF": "French Guiana", "GP": "Guadeloupe", "GR": "Greece", "HR": "Croatia",
                 "HU": "Hungary", "IE": "Ireland", "IN": "India", "IR": "Iran", "IS": "Iceland", "IT": "Italy",
                 "JP": "Japan", "KR": "South Korea", "LB": "Lebanon", "LT": "Lithuania", "LU": "Luxembourg",
                 "LV": "Latvia", "MA": "Morocco", "MQ": "Martinique", "MT": "Malta", "MX": "Mexico", "MY": "Malaysia",
                 "NC": "New Caledonia", "NL": "Netherlands", "NO": "Norway", "NZ": "New Zealand", "PE": "Peru",
                 "PH": "Philippines", "PL": "Poland", "PT": "Portugal", "PY": "Paraguay", "RE": "Réunion",
                 "RO": "Romania", "RS": "Serbia", "RU": "Russia", "SE": "Sweden", "SG": "Singapore", "SI": "Slovenia",
                 "SK": "Slovakia", "TH": "Thailand", "TN": "Tunisia", "TR": "Türkiye", "TW": "Taiwan", "UA": "Ukraine",
                 "UY": "Uruguay", "ZA": "South Africa"}

_COUNTRY_CODE_RE = re.compile(r"[A-Z]{2}")


def normalize_country_code(country_code) -> str:
    """The code in the form of the APK, or "" when it can't be a country code."""
    if not isinstance(country_code, str):
        return ""
    code = country_code.strip().upper()
    return code if _COUNTRY_CODE_RE.fullmatch(code) else ""


def countries_of_brand(brand) -> list:
    """(code, name) of the countries of a brand sorted by name, empty for an unknown brand."""
    codes = BRAND_COUNTRIES.get(brand, ()) if isinstance(brand, str) else ()
    return sorted(((code, COUNTRY_NAMES[code]) for code in codes), key=lambda country: country[1])
