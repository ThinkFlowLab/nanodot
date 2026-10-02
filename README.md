# nanodot
nanodot is a minimal, open-source AI assistant, designed for persistent memory and proactive task execution.

## Install with npm on macOS or Linux

Requires Node.js 22 or newer. After the first npm release is published:

```sh
npm install -g nanodot
nanodot --help
```

Or run without a global installation:

```sh
npx nanodot --help
```

The launcher uses an existing Python 3.11+ when available. Otherwise, its first
run downloads a private Python runtime automatically. macOS Intel and Apple
Silicon are supported. Automatic setup needs internet access and `curl`, which
is included with macOS. Later runs reuse the installed runtime.

Runtime downloads live in `~/Library/Caches/nanodot/npm` on macOS and
`~/.cache/nanodot/npm` on Linux; `XDG_CACHE_HOME` overrides the cache location.
Application data uses `~/.nanodot`, or the directory set by `NANODOT_HOME`.

See [npm packaging and release checks](docs/npm-packaging.md) for testing a
package before publication.
