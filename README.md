# Apple Productivity CLI

CLI-first local access to Apple Mail, Calendar, and Reminders on macOS, with an optional MCP adapter for clients that prefer MCP.

## Quick Start

```sh
git clone <this-repo> cli-apple-productivity
cd cli-apple-productivity
./apple-productivity doctor
./apple-productivity mail-accounts list
```

The CLI emits compact JSON by default for tools and agents. Add `--pretty` when reading output yourself.

```sh
./apple-productivity mail-messages list --mailbox-name INBOX --limit 5 --pretty
./apple-productivity mail triage --unread-only --limit 10
./apple-productivity calendar agenda --days 7
./apple-productivity day plan
```

## Optional PATH Install

```sh
./install.sh
apple-productivity doctor
```

Make sure `~/.local/bin` is on your `PATH`.

Optional shell completions:

```sh
mkdir -p ~/.zfunc ~/.local/share/bash-completion/completions
apple-productivity completions zsh > ~/.zfunc/_apple-productivity
apple-productivity completions bash > ~/.local/share/bash-completion/completions/apple-productivity
```

## Docs

Full CLI, MCP, permissions, and verification docs live in:

[plugins/apple-productivity/README.md](plugins/apple-productivity/README.md)
