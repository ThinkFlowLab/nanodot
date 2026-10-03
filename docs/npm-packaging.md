# npm packaging

The npm package bundles the Python source from the same checkout and exposes
`nanodot` through a Node launcher. It has no npm dependencies or installation
hooks; installation also works with `--ignore-scripts`. Runtime preparation
happens on the first command that needs it. The bundled CLI is standard-library
only — `pyproject.toml` declares no runtime Python dependencies — so the
package ships the `.py` sources and needs no `pip install` step at run time.

The launcher reuses Python 3.11+ from PATH or downloads Python 3.12 using uv,
pinned to uv 0.12.21 via `bin/uv-manifest.json`. The manifest names the uv
release tarball for each supported platform (macOS and Linux, arm64 and x64,
glibc) and its SHA-256; the launcher downloads the tarball from the immutable
GitHub release, verifies the digest before extraction, and never executes a
downloaded script. A mismatch fails closed with nothing published to the cache.
uv and Python are stored in nanodot's runtime cache. The `--no-bin` Python
installation keeps setup from changing shell profiles or creating global Python
commands. The download step requires `curl` and `tar` on PATH. The CLI receives
the original arguments, working directory, environment, signals and exit status.
Its background runner inherits the bundled source path.

## Validate and try a package

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run test:package
npm run test:bootstrap
npm pack
npm install -g ./nanodot-0.1.0.tgz --ignore-scripts
nanodot --help
```

The tests cover reused Python, automatic setup and cache reuse, failed setup,
digest-mismatch and failed-extraction fail-closed behavior, manifest shape,
argument/environment forwarding, cooperative termination, and installation of
the packed archive outside the source checkout. The bootstrap smoke downloads
the real runtime — which also validates the pinned digests in
`bin/uv-manifest.json` against the real release — checks a cached restart, and
verifies anonymous GitHub TLS. It requires internet access. CI runs these
checks on Linux and macOS. When the MVP is present, the package smoke also
starts and stops its background runner. The existing Python suite remains the
application behavior check.

To move uv to a new version, update `uvVersion`, `baseUrl`, and every asset
digest in `bin/uv-manifest.json` from the release page (the GitHub API exposes
each asset's `sha256` digest), then run `npm run test:bootstrap` to validate
the pins against the real download.

## Release

Keep `package.json`, `pyproject.toml`, and `src/nanodot/__init__.py` versions in
sync. Run both Python and npm checks against the final source, inspect
`npm pack --dry-run`, and publish the tested package with an authorized npm
account:

```sh
npm publish ./nanodot-0.1.0.tgz --access public
```

Publication is a separate maintainer action. This repository does not publish
packages automatically. The first full CLI release also needs the reviewed MVP
from PR #28 on main.
