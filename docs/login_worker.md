# Login through your own stelloauth worker

The automatic login opens the Stellantis login page in a local Playwright browser. When that browser gets rejected,
PSACC can ask a login service instead: [stelloauth](https://github.com/tamcore/stelloauth), or another service with
the same `POST /worker` API. PSACC only calls the service, it doesn't contain or start it.

It is **off by default**. With a URL set, the login order is: login worker → local Playwright browser → manual link.
When the worker fails, its message is shown. A rate limit of the worker (HTTP 429) is reported as such.

The local browser only takes over when the worker can't have tried your credentials or wasn't able to log in for
another reason, because a second failed login could lock your account:

| Answer of the worker | Next step |
|---|---|
| No answer in time, network error, HTTP 5xx, 429, 404, 405, 408 | local browser |
| Answer without a `message` (e.g. the error page of a proxy), or without a code | local browser |
| 4xx with a message saying the login form wasn't reached (bad request, service busy, browser backend unavailable, page or form not loading) | local browser |
| 4xx saying the session expired | local browser |
| Any other 4xx with a message, e.g. `authentication failed: ...` | **stop**, shows the message and the manual link |

stelloauth answers HTTP 400 to all its failures, so the message decides. A message PSACC doesn't know counts as a
rejection, even when the reason was something else (e.g. a captcha): try again or use the manual link.

## Security: your password goes to this service

**The email and password of your Stellantis account are sent to the URL you configure.** Whoever runs the service can
read them and log in to your account, and with it locate your car.

- **Only use a service you host yourself.** Don't enter a public instance run by someone else.
- **Use `https://` unless the service is local.** With `http://` the password crosses the network unencrypted. PSACC
  warns on the login form and in the log for `http://` to a non-local address, but still sends it. Local means
  loopback, private and link-local IP addresses, names without a dot (e.g. a container of the same Compose project)
  and `.local`, `.lan`, `.internal`, `.home.arpa`.
- The password is not stored by PSACC and is not logged. The URL is only read from `config.ini`: it can't be changed
  through the `/settings` API and is left out of its answer. Protect `config.ini` like the rest of your setup, since
  whoever can edit it can redirect your next login.

## Setup

1. Run stelloauth on a machine you control (see its README, it comes with a Docker Compose file).
2. Fill the `[Login Worker]` section of `config.ini` (added the next time PSACC writes the file, you can also add it
   yourself) and restart PSACC:

   ```ini
   [Login Worker]
   url = http://stelloauth:8080
   ```

   Give the base URL of the service. PSACC calls `/worker` on it, the only endpoint that uses the PKCE challenge of
   PSACC: a code from the web page of stelloauth wouldn't be accepted.
3. Log in as usual from the settings page. It can take several minutes: PSACC waits up to 5 minutes for the worker.

## What is sent

```
POST <url>/worker
{"url": "<authorization URL of PSACC>", "email": "...", "password": "..."}
```

The answer is `{"code": "..."}` or, on failure, `{"message": "...", "code": <http status>}`. Redirects aren't
followed, so point the URL directly at the service.
