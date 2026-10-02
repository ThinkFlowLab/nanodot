# npm packaging

The npm package bundles the Python source from the same checkout and exposes
`nanodot` through a Node launcher. It has no npm dependencies or installation
hooks; installation also works with `--ignore-scripts`. Runtime preparation
happens on the first command that needs it.

The launcher reuses Python 3.11+ from PATH or downloads Python 3.12 using uv's
official installer, pinned to uv 0.12.21. uv and Python are stored in nanodot's
runtime cache. The unmanaged installer and `--no-bin` Python installation keep
setup from changing shell profiles or creating global Python commands.
The CLI receives the original arguments, working directory, environment,
signals and exit status. Its background runner inherits the bundled source path.

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
argument/environment forwarding, cooperative termination, and installation of
the packed archive outside the source checkout. The bootstrap smoke downloads
the real runtime, checks a cached restart, and verifies anonymous GitHub TLS.
It requires internet access. CI runs these checks on Linux and macOS. When the
MVP is present, the package smoke also starts and stops its background runner.
The existing Python suite remains the application behavior check.

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
