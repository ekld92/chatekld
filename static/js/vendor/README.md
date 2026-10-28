# Vendored browser dependencies

`dompurify.es.mjs` is DOMPurify 3.4.14's unminified ESM distribution, fetched
from the npm registry tarball `dompurify-3.4.14.tgz`. The trailing source-map
comment was removed because the map is not shipped. Runtime code is deliberately
served from this repository; the application does not load a CDN or
`node_modules`.

- npm integrity: `sha512-dVoH9z+MY+C9IilgGCk3YfFqjLi3fChm2OiKJMzh6axrJ5qwxqWaZamgmHrpv22CN/KdbZJuGEGgfQoL00LTdg==`
- vendored module SHA-256: `1bf5dab751a7294af9d405a147ccd3adaf4c257f380955ec3bca973717cf40f6`
- license: Apache-2.0 OR MPL-2.0; see `DOMPURIFY-LICENSE.txt`
- upstream: <https://github.com/cure53/DOMPurify/tree/3.4.14>

When updating, review the upstream changelog, re-run the adversarial sanitizer
corpus, update both hashes, and retain the license file.
