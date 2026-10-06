# CookieCloud `/export` recordings

Recorded from the deployment's Go server, not synthesised. The cookie **values**
are replaced with `synthetic-*` placeholders and every `expirationDate` is pinned
to `4102444800.0` (2100-01-01) so no credential enters the repository. Names,
domains, paths, `secure`/`httpOnly`/`hostOnly`/`sameSite` and the row order are
exactly what the server sent.

| file | command |
|---|---|
| `export-zhihu-domain.json` | `curl -sS -H "Authorization: Bearer $T" "$URL/export/$U?format=raw&domain=zhihu.com"` |
| `export-all-domains.json` | `curl -sS -H "Authorization: Bearer $T" "$URL/export/$U?format=raw"` |

- Server: `cookiecloud-server` 0.3.1 (the Go server, `cookiecloud:8088`)
- Endpoint: `https://cookiecloud.example.ts.net:8088`
- Recorded: 2026-10-06T05:12:56Z
- `$U` is a dedicated uuid holding only the sites the reader needs; `$T` is
  `PEARSS_COOKIECLOUD_ADMIN_TOKEN`. Neither is stored here.

## Why two recordings

The pair is the evidence for two claims the adapter's tests rest on:

1. `domain=` is a **label-suffix** filter applied by the *server*: the
   `zhihu.com` needle returned `.zhihu.com`, `www.zhihu.com` and
   `zhuanlan.zhihu.com`.
2. The adapter does **not** re-filter: the x.com cookies present in the
   unfiltered body survive, which
   `tests/unit/test_cookiecloud_contract.py::test_the_adapter_does_not_re_filter_by_domain`
   pins.

Re-record with the same commands when the server's export shape changes. A
recording is worth exactly what its provenance says.
