# THD Markdown Web

Read websites as Markdown using sitemap navigation and an offline cache.

Built with GTK 4, GtkSourceView 5, WebKitGTK 6 and Python. This checkout includes
the source loader and local Vault document client; no private services or other
checkouts are required. MIT licensed. Bundled Sora and Atkinson Hyperlegible fonts
retain their included SIL Open Font Licenses.

## Install on Linux

Requires Python 3.10+, GTK 4.10+, GtkSourceView 5, WebKitGTK 6, and PyGObject.
On Ubuntu:

```sh
sudo apt install python3-venv python3-gi python3-markdown-it gir1.2-gtk-4.0 gir1.2-gtksource-5 gir1.2-webkit-6.0 desktop-file-utils
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
./scripts/install-user.py
./scripts/run.sh
```

The installer writes only the current user's launcher, menu entry, and fonts.
Keep the checkout in place, or reinstall after moving it. It does not change
default file associations.

## Use

Write, Split, and Read modes are available in the editor. Open local UTF-8 files,
or use Sources for `gs://bucket/path` and `dropbox:///folder/file.md`. GCS uses
your configured Google Cloud CLI account; Dropbox uses `DROPBOX_ACCESS_TOKEN`
from your environment. No credentials are bundled. AWS S3 remains disabled.
Cloud originals are read-only; edits save as local copies.

The web reader accepts a domain, HTTPS page, or XML sitemap. Published sitemap
pages fill a searchable sidebar. HTML is converted to Markdown before rendering;
embedded images, scripts, styles, and fonts are never downloaded by the reader.
Cache all pages builds an offline collection. Cached reads make no network calls;
Refresh is explicit and displays the content timestamp. JavaScript-only content
can be incomplete; forms, authenticated web sessions, PDF, and media are absent.

The Directory of Shame exporter records dated, scoped missing-sitemap evidence.
Network, TLS, permission, parser, and discovery-limit failures are unclassified.
It retains known custom sitemap locations and marks repaired entries resolved.
Exports contain domain/endpoint evidence, never cached page contents or account
configuration. The Publish missing-sitemap check button requests an independent public recheck
through the directory repository's GitHub Actions workflow. Publishing requires
GitHub CLI authentication with publisher access; the desktop cache itself is never
uploaded. The workflow publishes dated evidence through GitHub Pages.

## Vault documents

Open from Vault and Save to Vault use the local Hidden Door Vault app's document
bridge. Unlock the Vault there; these apps never collect the master password or
receive encryption keys. Document metadata is listed separately from credentials.
The Vault must support the document bridge and have attachment storage configured.
Updates use revision checks so a newer Vault item cannot be silently overwritten.
Vault documents open in memory and are not put in the web cache. Files are limited
to 16 MiB; the Markdown editor accepts UTF-8 text up to 2 MiB.

## Test

```sh
sudo apt install xvfb xdotool dbus-x11 python3-pil openssl
./scripts/test.sh
```

Tests use synthetic documents and mocked services. GUI acceptance uses an isolated
display and a temporary HTTPS fixture. No Google, Dropbox, or real Vault account
is accessed. Generated QA files are ignored by Git.

## Public directory hosting

The Pages workflow builds only from public records under `directory/` and isolated
CI checks. Run **Publish directory** with a domain input to check a new site;
previously submitted domains are rechecked weekly. Inputs and observations are
public: submit only public domains. Checks reject private/reserved network targets.
No local desktop cache, Vault item, account configuration, or credential is uploaded.

Enable GitHub Pages with the Actions source. An optional repository variable
`PAGES_DOMAIN` adds the custom hostname to the generated site. Keep it unset until
that hostname's DNS is ready. Only standard hosted runners are used.
