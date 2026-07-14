# Setting up the institutional layer for YOUR library

## Which off-campus family is your library? (it decides your `auth:` config)

Every library wires off-campus access as one of four families. Identify yours first — it
decides whether the built-in form login covers you or you need a custom `login()`:

| Family | How you recognize it | What to do |
|---|---|---|
| **EZproxy** | publisher hosts get rewritten to `host-with-dashes.proxy.yourlib.edu` | `auth.family: form` with EZproxy selectors — `login_path: "/login"`, `user_selector: "input[name='user']"`, `pass_selector: "input[name='pass']"`, no captcha keys |
| **Custom portal** (NetScaler, homegrown) | a bespoke "remote reader authentication" page | `auth.family: form` — point the selectors at your portal's login form (devtools). Django-style gates with a numeric CAPTCHA: also set the three `captcha_*` keys; the offline OCR handles it |
| **OpenAthens / Shibboleth** | login bounces through `login.openathens.net` or your university SSO (SAML) | `auth.family: custom` — implement `login()` to drive the SSO redirect chain once in the headful browser; the persistent profile then keeps the session |
| **VPN** | you install a VPN client and publishers just see campus IP | *no proxy layer needed at all* — run only the OA/TDM half, publishers serve PDFs directly |

In all four cases the *rest* of the machinery (route shapes, entitlement table, access log,
throttle, response classifier, the full LWW/Ovid flow) is family-agnostic — authentication
is the only part that is yours.

### `auth.persist_cookies` — keeping the session across runs

The gate's session cookie and the proxy-authorization cookie are usually session-only
(Chromium won't write them to disk), so `library_session.py` saves them to your secret
store after login and re-injects them next run. Log in once in a normal browser, open
devtools → Application → Cookies, and map each cookie name to its exact domain in
`auth.persist_cookies`. Two cookies is the common shape: one on the remote-auth host, one
wildcarded on the parent domain (that one is the proxy authorization).

⚠ On some proxies that authorization is granted **per proxy subdomain** via a JS handshake
on first visit — being authorized on one publisher's subdomain does not authorize
another's. The code already handles this (it re-submits the login form on the bounce page
itself, which is what completes the handshake); it's mentioned here so a bounce-to-login on
a *new* publisher doesn't read as a broken session.

## Finding your library's endpoints

The proxy layer needs three values in `config.yaml`. None of them are secret — they're your
library's public infrastructure. Where to find them:

- **`sfx_base`** (link resolver): your library portal → *e-resources* / *databases* → *link
  resolver* / *SFX* / *Find it @ ...*. It's an OpenURL endpoint; the DOI goes in `id=doi:`.
- **`remote_auth_base`** (off-campus login gate): the page you log into to read journals
  from home. Often a Citrix NetScaler / *remote reader authentication* / *remote access* portal.
- **`proxy_suffix`** (the rewrite host): notice how a publisher URL changes when you open it
  through the proxy — `onlinelibrary.wiley.com` becomes something like
  `onlinelibrary-wiley-com.proxy.yourlib.edu:port`. The part after the dash-rewritten host
  is your `proxy_suffix`. **When in doubt, ask your librarian** — they'll know the EZproxy /
  proxy hostname.

If your library uses **EZproxy** (very common), the rewrite is usually `dots→dashes` +
a fixed proxy suffix, exactly the pattern `_proxy_host()` implements.
