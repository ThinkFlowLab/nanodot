"""The loopback UI server — an unprivileged consumer of the CLI (#100, #110).

The UI never becomes a second writer and never opens the stores: it binds
127.0.0.1 only, serves static shell assets from this package, and every
mutation or read beyond the shell rides a CLI subprocess (the DSH-tools
pattern). The runner's lifetime flock stays the only write path's owner.
"""

from nanodot.ui.server import (  # noqa: F401  (public surface)
    LOOPBACK, NanodotUI, UIHandler, UIServer, handler_exchange, serve,
)
