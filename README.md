# nanodot
nanodot is a minimal, open-source AI assistant, designed for persistent memory and proactive task execution.

## Store secrets locally

Use the hidden prompt to keep tokens out of shell history and process arguments:

```sh
nanodot config set github-token
```

For automation, pass `-` and supply the value on stdin from a secret manager:

```sh
secret-manager-command | nanodot config set github-token -
```

Secret names ending in `token`, `key`, `secret`, `password`, `passwd`, or `pat`
are routed to the secret store (hyphen/underscore separated, case insensitive).
`nanodot config list` masks secret values, including legacy plaintext entries;
setting or unsetting such an entry removes its legacy config copy.

Secrets are plaintext in `secrets.json` under `NANODOT_HOME` (default `~/.nanodot`),
protected by file permissions rather than encryption. Updates replace a private
0600 temporary sibling atomically and reject symlink stores. Handled failures
clean up the temporary file; an abrupt process kill can leave that private file
behind. Configured secret values are redacted recursively from nested data.
