# Third-party licenses

This project bundles, vendors, or depends on the following third-party software.
Their licenses are reproduced below.

## Bundled / vendored

### Octicons (icons)

GitHub Octicons — used for the in-app icons (inlined as SVG `<path>` data in the
templates; the source files are vendored under `static/vendor/octicons/`).
Project: https://github.com/primer/octicons

> MIT License
>
> Copyright (c) 2026 GitHub Inc.
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

GitHub's logo marks (`mark-github`, `logo-github`, `logo-gist`) are GitHub
trademarks and are **not** used by this project; they are governed separately by
https://github.com/logos rather than by the MIT license above.

### Pico CSS

Pico CSS v2.1.1 — `static/vendor/pico.min.css`.
Project: https://github.com/picocss/pico

> MIT License
>
> Copyright (c) 2019-2024 Pico
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

### htmx

htmx v2.0.10 — `static/vendor/htmx.min.js`.
Project: https://github.com/bigskysoftware/htmx
Licensed under the Zero-Clause BSD license, which requires no attribution;
listed here for completeness.

> Zero-Clause BSD
>
> Permission to use, copy, modify, and/or distribute this software for
> any purpose with or without fee is hereby granted.
>
> THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL
> WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES
> OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE
> FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY
> DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN
> AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
> OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

## Runtime dependencies (installed via pip, not bundled)

These are declared in `pyproject.toml` and installed into the virtualenv; they
are not redistributed as part of this repository. Each is under a permissive
license:

| Package | License |
| --- | --- |
| Flask (and its deps Werkzeug, Jinja2, click, itsdangerous, MarkupSafe, blinker) | BSD-3-Clause |
| requests (and its deps urllib3, idna, certifi, charset-normalizer) | Apache-2.0 / MIT / MPL-2.0 |
| python-dotenv | BSD-3-Clause |
| anthropic | MIT |

Run `pip show <package>` for the exact license text and copyright holder of any
installed dependency.
