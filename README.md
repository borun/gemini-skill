# gemini-skill

[![tests](https://github.com/borun/gemini-skill/actions/workflows/test.yml/badge.svg)](https://github.com/borun/gemini-skill/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An [Agent Skill](https://agentskills.io) that lets your coding agent **consult Google Gemini** for second opinions, code reviews and offloaded analysis, and **generate or edit images** with Nano Banana. It uses the official Gemini API with your own API key.

It works in Claude Code, OpenAI Codex, Cursor, GitHub Copilot, Gemini CLI, OpenCode, Goose, Amp, Devin Desktop (Windsurf), and any agent that can run a shell command.

```
You:    Consult Gemini Neon High about this migration plan.
Agent:  [gemini] neon-high → gemini-4-neon · effort high (accepted 2026-11-26) · $3/$15 per 1M tokens in/out · ~2,000 input tokens ≈ $0.0060
        [gemini] tokens in=2000 out=640 thought=1210 · cost ≈ $0.03
        Gemini 4 Neon says the backfill should run before the constraint is added. I agree: …
```

## Why this skill

- **It doesn't go stale.** Model names come from Google's live model list for your key, so `neon` always means the newest stable Neon. You get a one-line notice when Google ships a new version. Effort levels are judged by the API itself, and what it accepts or rejects is remembered.
- **It checks before it spends.** Every request is checked first: does the model exist for your key, is the effort level known to work, and what will the input cost? A cost guard ($0.50 by default) stops oversized requests until you agree.
- **It tells you the cost.** Every call ends with the token count and an estimated cost. Scheduled price changes apply automatically on their date.
- **It keeps your key safe.** The key comes from an environment variable or your system keychain, and is never pasted into the chat or printed.
- **It needs nothing extra.** It's one Python file using only the standard library, and runs on Python 3.9+ on macOS, Linux and Windows.

## Requirements

- Python 3.9 or later (`python3`; on Windows, `python`).
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).
  - Free-tier keys can use Hydrogen and Flash.
  - Neon, Carbon, Argon and all the image models need a key with billing enabled.
- Outbound HTTPS to `generativelanguage.googleapis.com`.

## 1. Install

### Claude Code: as a plugin (recommended)

In a Claude Code session:

```
/plugin marketplace add borun/gemini-skill
/plugin install gemini@gemini-skill
```

Or from a shell:

```bash
claude plugin marketplace add borun/gemini-skill
```

```bash
claude plugin install gemini@gemini-skill
```

To update later, run `claude plugin marketplace update gemini-skill`, then `claude plugin update gemini@gemini-skill`.

### Claude Code: as a plain skill

```bash
git clone https://github.com/borun/gemini-skill.git ~/src/gemini-skill
```

```bash
ln -s ~/src/gemini-skill/skills/gemini ~/.claude/skills/gemini
```

To update later, run `git -C ~/src/gemini-skill pull`.

### Other agents

With the [`skills` CLI](https://github.com/vercel-labs/skills), which needs Node.js 22.20+, this detects your installed agents and asks which ones to install into:

```bash
npx skills add borun/gemini-skill -g
```

With the [GitHub CLI](https://cli.github.com/manual/gh_skill_install) (replace `codex` with your agent):

```bash
gh skill install borun/gemini-skill gemini --agent codex --scope user
```

Or by hand: clone the repo and link (or, on Windows, copy) `skills/gemini` into your agent's skills folder.

```bash
git clone https://github.com/borun/gemini-skill.git ~/src/gemini-skill
```

```bash
mkdir -p ~/.agents/skills && ln -s ~/src/gemini-skill/skills/gemini ~/.agents/skills/gemini
```

`~/.agents/skills/` is the shared folder most agents read. These are the user-level folders as documented by each vendor on 2026-11-26:

| Agent | User-level skills folder | Docs |
|---|---|---|
| Claude Code | `~/.claude/skills/` | [docs](https://code.claude.com/docs/en/skills) |
| OpenAI Codex | `~/.agents/skills/` | [docs](https://developers.openai.com/codex/skills) |
| Gemini CLI | `~/.gemini/skills/` or `~/.agents/skills/` | [docs](https://geminicli.com/docs/cli/skills/) |
| GitHub Copilot CLI | `~/.copilot/skills/` or `~/.agents/skills/` | [docs](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills) |
| GitHub Copilot in VS Code | `~/.copilot/skills/` (project: `.github/skills/`) | [docs](https://code.visualstudio.com/docs/agent-customization/agent-skills) |
| Cursor | `~/.cursor/skills/` or `~/.agents/skills/` | [docs](https://cursor.com/docs/context/skills) |
| OpenCode | `~/.config/opencode/skills/` or `~/.agents/skills/` | [docs](https://opencode.ai/docs/skills) |
| Goose | `~/.agents/skills/` | [docs](https://goose-docs.ai/docs/guides/context-engineering/using-skills) |
| Amp | `~/.config/agents/skills/` or `~/.agents/skills/` | [docs](https://ampcode.com/docs/customize/skills) |
| Devin Desktop (Windsurf) | `~/.config/devin/skills/` or `~/.agents/skills/` | [docs](https://docs.devin.ai/desktop/cascade/skills) |

For a single project, use the project-level folder instead: `.claude/skills/`, `.agents/skills/` or `.github/skills/`.

Claude Code fills in `${CLAUDE_SKILL_DIR}` in SKILL.md automatically. Other agents are told in SKILL.md to use the skill's own folder instead.

**An agent without skill support** can still use the command-line tool. Add a line like this to your `AGENTS.md`:

```
To consult Gemini or make images with it, follow ~/src/gemini-skill/skills/gemini/SKILL.md (the script is in its scripts/ folder).
```

**Not supported: claude.ai chat skill uploads.** Their sandbox has no safe place for your API key, and by default it can't reach the Gemini API host.

## 2. Add your API key

Store the key once, in your own terminal. Don't paste it into an agent's chat.

**macOS (Keychain).** This prompts for the key; nothing is shown as you type:

```bash
security add-generic-password -a "$USER" -s gemini-api-key -w
```

**Linux (Secret Service)**, which needs `secret-tool`, part of `libsecret-tools`:

```bash
secret-tool store --label="Gemini API key" service gemini-api-key
```

**Any platform (environment variable):** set `GEMINI_API_KEY` in your shell profile. `GOOGLE_API_KEY` also works. On Windows: `setx GEMINI_API_KEY "your-key"`.

Then check the setup. The skill's folder is wherever you installed it; with the plain install above it's `~/src/gemini-skill/skills/gemini`:

```bash
python3 ~/src/gemini-skill/skills/gemini/scripts/gemini.py doctor
```

`doctor` shows where the key was found (never the key itself), your defaults, the catalog in use, and whether the API is reachable.

## 3. Use it

Just ask your agent:

- "Consult Gemini Neon High: is this caching strategy safe under concurrent writes?"
- "Ask Gemini Carbon to review this diff for bugs."
- "Get a quick sanity check from Gemini Flash on this regex."
- "Generate a 16:9 hero image for the landing page with Nano Banana Pro."

The skill gives the agent a standard procedure:
1. It turns your words into a model and effort.
2. It writes a self-contained prompt with only the files the question needs, never secrets.
3. It runs the preflight checks.
4. It tells you, rather than silently substituting, if a model is missing or an effort level is rejected. For example, Carbon has no `xhigh`, so the agent offers `high`.
5. It reports Gemini's answer, its own assessment of that answer, and the cost.

### Models and effort

| Alias | Family | Good for |
|---|---|---|
| `carbon` | Gemini Carbon | Strongest reasoning: the hardest reviews and design questions. Slowest. |
| `neon` | Gemini Neon | Fast frontier model: a strong all-round second opinion. |
| `argon` | Gemini Argon | Efficient reasoning: fewer tokens than Carbon. |
| `hydrogen` | Gemini Hydrogen | Everyday work at a lower price. The built-in default; works with free-tier keys. |
| `flash` | Gemini Flash | Quick, cheap checks. Works with free-tier keys. |
| `lite` | Gemini Flash-Lite | The cheapest option, for trivial tasks. |
| `nb` | Nano Banana | The default image model. |
| `pro` | Nano Banana Pro | Text-heavy or professional images (with `image`). |

Add an effort level after a hyphen: `neon-high`, `carbon-medium`, `hydrogen-xhigh`. Run `gemini.py models` to see the current versions, the effort levels each supports (`*` marks the default) and prices. A full model ID such as `gemini-4-neon-high` pins an exact version.

## Command-line reference

The same commands work without an agent. `gemini.py` stands for `python3 <skill folder>/scripts/gemini.py`.

```
gemini.py ask "QUESTION" [-f FILE ...] [-m MODEL[-EFFORT]] [-t LEVEL] [-s SYSTEM] [-c ID] [--yes] [--raw]
gemini.py image "PROMPT" [-o OUT] [-i IMAGE ...] [-m MODEL] [--aspect 16:9] [--size 2K] [--yes]
gemini.py check [MODEL[-EFFORT]] [--image] [--probe] [--refresh]
gemini.py models [--all] [--refresh]
gemini.py doctor
gemini.py catalog
```

| Command | What it does |
|---|---|
| `ask` | Sends a question. Use `-f` for context files (`-f -` reads stdin, e.g. `git diff \| gemini.py ask "Review" -f -`; images are attached as images). Use `-c ID` to follow up in the same conversation; the ID is printed on stderr. |
| `image` | Generates an image, or edits one with `-i` (up to 14 reference images). Images are saved as JPEG and existing files are never overwritten. |
| `check` | Previews a request without sending a prompt. `--probe` confirms an effort level live; it costs a fraction of a cent, and a rejection is free. |
| `models` | Lists each alias's current model, effort levels, price and role. `--all` lists every model ID your key can use. |
| `doctor` | Checks the key, config, catalog and API access. |
| `catalog` | Prints the path of your editable price catalog, creating it if needed. |

The answer goes to stdout. Status lines (`[gemini] …`) and errors go to stderr. The exit code is non-zero on failure.

## Configuration

| Setting | `~/.config/gemini-skill/config.json` key | Environment variable | Built-in default |
|---|---|---|---|
| Default text model | `default_model` | `GEMINI_DEFAULT_MODEL` | `hydrogen` |
| Default image model | `default_image_model` | `GEMINI_DEFAULT_IMAGE_MODEL` | `nb` |
| Cost guard (USD of estimated input) | `max_input_cost` | `GEMINI_MAX_INPUT_COST` | `0.50` |

For example, with a billing-enabled key:

```json
{ "default_model": "neon-high" }
```

Environment variables override the config file. `$XDG_CONFIG_HOME` and `$XDG_CACHE_HOME` are respected.

## How it stays current

- **Model list.** The model list is fetched from `GET /v1beta/models` and cached for a day in `~/.cache/gemini-skill/state.json`. An alias picks the highest version that exactly matches its family pattern, e.g. `gemini-<version>-neon`, so previews and TTS, live and image variants are never picked by mistake. If the API can't be reached, the cached list is used, then built-in names.
- **Effort levels.** The API is the source of truth. An accepted or rejected level is recorded and shown by `check` and `models`, and a rejection comes with the nearest level known to work.
- **Prices.** Prices aren't available from the API, so they live in [`skills/gemini/catalog.json`](skills/gemini/catalog.json) with a `verified_on` date and any scheduled `changes`.
  - When the prices are over 30 days old, or a new model has no price, the script says so. The agent then refreshes the catalog from Google's pricing page into your editable copy at `~/.config/gemini-skill/catalog.json`.
  - Whichever catalog was checked more recently wins, so pulling a newer version of this repo updates your prices too.

Pull requests that refresh `catalog.json` are welcome. Update `verified_on` and cite the pricing page.

## Costs, privacy and terms

- You pay Google for what you use, at the rates in your Google AI Studio account. Thinking tokens bill as output. Cost estimates here are approximate: cached tokens and long-context tiers aren't modelled.
- Everything you send goes to Google. SKILL.md tells the agent to send only what the question needs, and never `.env` files, keys or credentials. Review what your agent sends.
- This skill uses the official Gemini API with an API key. It doesn't use or reuse an Antigravity or Gemini app subscription login. Google treats using those logins from third-party tools as a terms-of-service violation.
- In Claude Code, each call asks for permission to run the script, because it sends data off your machine. You can add an allow rule in your settings if you prefer.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no API key found` | Store the key as in step 2, then run `doctor`. |
| `API error 403` or `429` | The model may need a billing-enabled key (Neon, Carbon, Argon, images), or you hit a rate limit. Try `-m hydrogen`, or enable billing. |
| `not a known alias or a model in Google's live list` | Run `models --all` to see what your key can use. |
| `refresh the catalog` | Ask your agent to refresh it (the steps are in SKILL.md), or pull the latest version of this repo. |
| The answer looks cut off | Carbon and Argon cap answers at about 32K tokens. Use Neon or Hydrogen for very long outputs. |

## Development

```
skills/gemini/
├── SKILL.md            # instructions and the standard procedure the agent follows
├── catalog.json        # prices and documented effort levels, with verified_on
└── scripts/gemini.py   # the client (standard library only)
tests/test_gemini.py    # offline tests against a fake Gemini API
.claude-plugin/         # Claude Code plugin and marketplace manifests
```

Run the tests. They need no key or network, and cost nothing:

```bash
python3 -m unittest discover -s tests -v
```

CI runs them on Linux, macOS and Windows.

## Disclaimer

This is an independent project, not affiliated with or endorsed by Google or Anthropic. Gemini and Nano Banana are trademarks of Google LLC. Model names, effort levels and prices reflect Google's documentation as of the catalog's `verified_on` date. Check [Google's pricing page](https://aistudio.google.com/docs/pricing) for current rates.

## License

[MIT](LICENSE)
