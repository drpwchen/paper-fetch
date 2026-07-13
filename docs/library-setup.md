# Setting up the institutional layer for YOUR library

## Which off-campus family is your library? (adapt `login()` accordingly)

Every library wires off-campus access as one of four families. Identify yours first — it
decides how much of `login()` you need to write:

| Family | How you recognize it | What to implement |
|---|---|---|
| **EZproxy** | publisher hosts get rewritten to `host-with-dashes.proxy.yourlib.edu` | mostly nothing beyond `proxy_suffix` + a form login — the closest fit to this codebase |
| **OpenAthens / Shibboleth** | login bounces through `login.openathens.net` or your university SSO (SAML) | drive the SSO redirect chain once in the headful browser; the persistent profile then keeps the session |
| **VPN** | you install a VPN client and publishers just see campus IP | *no proxy layer needed at all* — run only the OA/TDM half, publishers serve PDFs directly |
| **Custom portal** (NetScaler, homegrown) | a bespoke "remote reader authentication" page | write the login walk against that portal — this repo's stub documents one worked example |

In all four cases the *rest* of the machinery (route shapes, entitlement table, access log,
throttle, response classifier) is family-agnostic — `login()` is the only part that is yours.

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
