"""The part of oauth2-client 1.4.2 this project uses: the package is no more maintained.

From credentials_manager.py of https://github.com/antechrestos/OAuth2Client, by Benjamin Einaudi, under the
Apache License 2.0, see oauth2_client_LICENSE.txt. Changes: only the token and request part is kept (no local
redirect server, no other grants), the code is adapted to the linters of this project. The behaviour is the same.
"""
import base64
import logging
import warnings
from http import HTTPStatus
from typing import Any, Callable, Optional
from urllib.parse import quote

import requests
from requests import Response
from urllib3.exceptions import InsecureRequestWarning

_logger = logging.getLogger(__name__)


class OAuthError(Exception):
    def __init__(self, status_code: HTTPStatus, error: str, error_description: Optional[str] = None):
        super().__init__()
        self.status_code = status_code
        self.error = error
        self.error_description = error_description

    def __str__(self) -> str:
        return '%d  - %s : %s' % (self.status_code.value, self.error, self.error_description)


class ServiceInformation:
    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def __init__(self, authorize_service: Optional[str], token_service: Optional[str], client_id: str,
                 client_secret: Optional[str], scopes: list, verify: bool = True):
        self.authorize_service = authorize_service
        self.token_service = token_service
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self.verify = verify

    @property
    def authorization_header(self):
        credentials = bytes('%s:%s' % (self.client_id, self.client_secret), 'UTF-8')
        return 'Basic %s' % base64.b64encode(credentials).decode('UTF-8')

    @property
    def public_api(self):
        return self.client_secret is None


class CredentialManager:
    def __init__(self, service_information: ServiceInformation, proxies: Optional[dict] = None,
                 user_agent: Optional[str] = None):
        self.service_information = service_information
        self.proxies = proxies if proxies is not None else {"http": '', "https": ''}
        self.user_agent = user_agent
        self.refresh_token = None
        self._session = None
        if not service_information.verify:
            warnings.filterwarnings('ignore', 'Unverified HTTPS request is being made.*', InsecureRequestWarning)

    @staticmethod
    def _handle_bad_response(response: Response):
        try:
            error = response.json()
            raise OAuthError(HTTPStatus(response.status_code), error.get('error'), error.get('error_description'))
        except OAuthError:
            raise
        except BaseException as ex:  # pylint: disable=broad-exception-caught
            _logger.exception('_handle_bad_response - error while getting error as json - %s - %s', type(ex), ex)
            raise OAuthError(HTTPStatus(response.status_code), 'unknown_error', response.text) from ex

    def generate_authorize_url(self, redirect_uri: str, state: str, **kwargs) -> str:
        parameters = {"client_id": self.service_information.client_id,
                      "redirect_uri": redirect_uri,
                      "response_type": 'code',
                      "scope": ' '.join(self.service_information.scopes),
                      "state": state,
                      **kwargs}
        return '%s?%s' % (self.service_information.authorize_service,
                          '&'.join('%s=%s' % (k, quote(v, safe='~()*!.\'')) for k, v in parameters.items()))

    def _grant_refresh_token_request(self, refresh_token: str) -> dict:
        return {"grant_type": "refresh_token",
                "scope": ' '.join(self.service_information.scopes),
                "refresh_token": refresh_token}

    def _refresh_token(self):
        payload = self._grant_refresh_token_request(self.refresh_token)
        try:
            self._token_request(payload, False)
        except OAuthError as err:
            if err.status_code == HTTPStatus.UNAUTHORIZED:
                _logger.debug('refresh_token - unauthorized - cleaning token')
                self._session = None
                self.refresh_token = None
            raise err

    def _token_request(self, request_parameters: dict, refresh_token_mandatory: bool):
        headers = self._token_request_headers(request_parameters['grant_type'])
        if self.service_information.public_api:
            request_parameters["client_id"] = self.service_information.client_id
        else:
            headers['Authorization'] = self.service_information.authorization_header
        # no timeout, as in the library: the same behaviour first, a timeout would be a change of its own
        response = requests.post(self.service_information.token_service,  # pylint: disable=missing-timeout
                                 data=request_parameters,
                                 headers=headers,
                                 proxies=self.proxies,
                                 verify=self.service_information.verify)
        if response.status_code != HTTPStatus.OK.value:
            CredentialManager._handle_bad_response(response)
        else:
            _logger.debug(response.text)
            self._process_token_response(response.json(), refresh_token_mandatory)

    def _process_token_response(self, token_response: dict, refresh_token_mandatory: bool):
        self.refresh_token = token_response['refresh_token'] if refresh_token_mandatory \
            else token_response.get('refresh_token')
        self._access_token = token_response['access_token']

    @property
    def _access_token(self) -> Optional[str]:
        authorization_header = self._session.headers.get('Authorization') if self._session is not None else None
        if authorization_header is not None:
            return authorization_header[len('Bearer '):]
        return None

    @_access_token.setter
    def _access_token(self, access_token: str):
        if self._session is None:
            self._session = requests.Session()
            self._session.proxies = self.proxies
            self._session.verify = self.service_information.verify
            self._session.trust_env = False
            if self.user_agent:
                self._session.headers.update({'User-Agent': self.user_agent})
        if access_token is not None and len(access_token) > 0:
            self._session.headers.update({"Authorization": 'Bearer %s' % access_token})

    def get(self, url: str, params: Optional[dict] = None, **kwargs) -> Response:
        kwargs['params'] = params
        return self._bearer_request(self._get_session().get, url, **kwargs)

    def post(self, url: str, data: Optional[Any] = None, json: Optional[Any] = None, **kwargs) -> Response:
        kwargs['data'] = data
        kwargs['json'] = json
        return self._bearer_request(self._get_session().post, url, **kwargs)

    def _get_session(self) -> requests.Session:
        if self._session is None:
            raise OAuthError(HTTPStatus.UNAUTHORIZED, 'no_token', "no token provided")
        return self._session

    def _bearer_request(self, method: Callable[..., Response], url: str, **kwargs) -> Response:
        if kwargs.get('headers') is None:
            kwargs['headers'] = {}
        _logger.debug("_bearer_request on %s - %s", method.__name__, url)
        response = method(url, **kwargs)
        if self.refresh_token is not None and self._is_token_expired(response):
            self._refresh_token()
            return method(url, **kwargs)
        return response

    @staticmethod
    def _token_request_headers(grant_type: str) -> dict:  # pylint: disable=unused-argument
        return {}

    @staticmethod
    def _is_token_expired(response: Response) -> bool:
        if response.status_code == HTTPStatus.UNAUTHORIZED.value:
            try:
                json_data = response.json()
                return json_data.get('error') == 'invalid_token'
            except ValueError:
                return False
        return False
