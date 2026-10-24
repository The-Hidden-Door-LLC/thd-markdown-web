"""Evidence-based public export; publishing transport is configured separately."""

import hashlib
from html import escape
import json

from .store import data_root, read_json, write_json, write_private, now


def observe(result):
    root = data_root()
    key = hashlib.sha256(result["site"].encode()).hexdigest()
    path = root / "records" / (key + ".json")
    previous = read_json(path)
    if result["missing_sitemap"]:
        record = {"site":result["site"], "status":"missing_sitemap", "reason":"No discoverable XML sitemap at the checked endpoints.",
                  "first_observed": previous.get("first_observed", now()) if previous else now(),
                  "checked_at":result["checked_at"], "checks":result["checks"]}
        write_json(path, record)
    elif previous and result["sitemaps"]:
        previous.update(status="resolved", checked_at=result["checked_at"], reason="A readable sitemap was found on recheck.", checks=[])
        write_json(path, previous)
    return export()


def export():
    root = data_root()
    records = [value for path in sorted((root / "records").glob("*.json")) if (value := read_json(path))]
    records.sort(key=lambda value: value["site"])
    public = {"title":"Directory of Shame", "criterion":"No discoverable sitemap at the checked endpoints. Network, TLS, permission, parser, and discovery-limit failures do not qualify.",
              "generated_at":now(), "entries":records}
    write_json(root / "index.json", public)
    rows = []
    for item in records:
        checks = "<br>".join(escape(check["url"]) + " · " + escape(check["outcome"]) for check in item["checks"])
        rows.append('<article><h2><a href="' + escape(item["site"], quote=True) + '">' + escape(item["site"]) + '</a></h2><p><strong>' + escape(item["status"].replace("_", " ")) + '</strong> · ' + escape(item["checked_at"]) + '</p><p>' + escape(item["reason"]) + '</p><details><summary>Checked endpoints</summary><p>' + checks + '</p></details></article>')
    html = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>Directory of Shame</title><style>body{background:#06070e;color:#dfe6ff;font:17px/1.6 system-ui;max-width:900px;margin:auto;padding:32px}h1,a{color:#e7c66a}article{background:#0a0d1a;border:1px solid #29334e;border-radius:12px;margin:24px 0;padding:24px;overflow-wrap:anywhere}small{color:#8d9abb}</style><h1>Directory of Shame</h1><p>Publish a sitemap. Make the open web readable.</p><p>' + escape(public["criterion"]) + '</p><small>Observations are dated, scoped checks—not proof that no sitemap exists anywhere. Resolved entries remain visible as resolved. No page contents, query strings, or account data are published.</small>' + ("".join(rows) if rows else "<p>No qualifying sites recorded.</p>") + '</html>'
    write_private(root / "index.html", html)
    return {"html":str(root / "index.html"), "json":str(root / "index.json"), "entries":len(records), "published":False}
