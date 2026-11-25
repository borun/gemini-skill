---
name: gemini
description: Consult Google Gemini for a second opinion, code review or analysis, or generate and edit images with Nano Banana, via the Gemini API. Use when asked to ask or consult Gemini.
license: MIT
compatibility: Needs python3 (3.9+), outbound HTTPS to generativelanguage.googleapis.com, and a Gemini API key.
---

# Gemini via the Gemini API

Engine: `python3 ${CLAUDE_SKILL_DIR}/scripts/gemini.py`. It uses only the Python standard library.

`${CLAUDE_SKILL_DIR}` is the folder that contains this SKILL.md. Claude Code fills it in for you; in other tools, use that folder's path. On Windows, use `python` instead of `python3`.

The script keeps itself current, so nothing here needs editing when Google ships or retires models:
- **Model names** are resolved against Google's live model list for the user's key, cached for a day. An alias such as `neon` always means the newest stable Neon.
- **Effort levels** are judged by the API. What it accepts or rejects is remembered and reported next time.
- **Prices** aren't published by the API, so they live in a dated `catalog.json`. Scheduled price changes apply on their date.

## Standard procedure for every Gemini request

1. **Turn the request into a model spec.** "Consult Gemini Neon High" becomes `-m neon-high`; "ask Carbon" becomes `-m carbon`. With no model named, leave out `-m` to use the configured default. For images, the default is `nb`; use `pro` for text-heavy images. If the user names something you don't recognise, run `models` and pick from it, or ask.
2. **Write a self-contained prompt.** Gemini sees none of this conversation. Give the goal, the constraints, the relevant files through `-f`, and the shape of answer you want (for example "list concrete bugs with file and line; say NONE if there are none"). Send only what the question needs. Never send `.env` files, keys, credentials, or anything the user called sensitive: the data leaves the machine.
3. **Run it.** Before spending anything, the script checks the request and prints a `[gemini]` preflight line on stderr:
   - The model must be in Google's live list for this key. Otherwise it stops and suggests close matches.
   - It states whether the effort level was accepted before, is in Google's docs, or is not yet verified.
   - It shows the price and an input-cost estimate. Above the cost guard ($0.50 by default) it stops: ask the user, then rerun with `--yes`.
4. **Act on what it reports. Never substitute silently.**
   - The model is missing, or the effort is rejected: tell the user and offer the suggestion it prints (for example, Carbon has no `xhigh`, so it suggests `high`). Rerun only with their OK, unless they already said to use the closest.
   - `"neon" now means X (was Y)`: Google released a newer version. Mention it in one line.
   - `refresh the catalog` (a price is missing, or prices are over 30 days old): do the refresh below, and tell the user what changed.
   - 403 or 429 errors: the hint names the likely cause (a model that needs a billing-enabled key, or a rate limit). Pass it on.
5. **Report back.** Give Gemini's answer, attributed ("Gemini 4 Neon says…"). Then give your own assessment: check its claims against the code, and say which points you agree or disagree with and why. Finish with the cost line the script printed.

Every call costs the user money. You may suggest a Gemini cross-check for a high-stakes change, but run it only after the user agrees, and never call it in a loop.

## Commands

```
python3 ${CLAUDE_SKILL_DIR}/scripts/gemini.py ask "QUESTION" [-f FILE ...] [-m MODEL[-EFFORT]] [-t LEVEL] [-s SYSTEM] [-c ID] [--yes]
python3 ${CLAUDE_SKILL_DIR}/scripts/gemini.py image "PROMPT" [-o OUT] [-i IMAGE ...] [-m MODEL] [--aspect 16:9] [--size 2K] [--yes]
python3 ${CLAUDE_SKILL_DIR}/scripts/gemini.py check [MODEL[-EFFORT]] [--image] [--probe]
python3 ${CLAUDE_SKILL_DIR}/scripts/gemini.py models [--all] [--refresh]
python3 ${CLAUDE_SKILL_DIR}/scripts/gemini.py doctor
python3 ${CLAUDE_SKILL_DIR}/scripts/gemini.py catalog
```

- `-m` takes an alias (`carbon`, `neon`, `argon`, `hydrogen`, `flash`, `lite`; for images `nb`, `pro`, `flash`, `lite`), optionally with an effort suffix (`neon-high`, `carbon-medium`). A full model ID pins an exact version, e.g. `gemini-4-neon-high`. `-t` sets the effort and overrides a suffix.
- Rough roles, which stay stable across versions: Carbon is the strongest reasoner (hardest reviews), Neon is fast frontier, Argon is efficient reasoning, Hydrogen is cheaper everyday work and works on free-tier keys, Flash is quick and cheap. Run `models` for the current versions, effort levels and prices.
- `-f -` reads stdin, e.g. `git diff | python3 ${CLAUDE_SKILL_DIR}/scripts/gemini.py ask "Review this diff" -f -`. Image files passed with `-f` are attached as images.
- The interaction id is printed on stderr. Pass it to `-c` to ask a follow-up in the same conversation.
- `check SPEC` previews a request without sending a prompt. `--probe` confirms an effort level live with a tiny prompt, which costs a fraction of a cent; a rejection is free.
- `doctor` checks the setup: where the key was found (never its value), the config, the catalog and API access. Run it first when something fails.
- Images come back as JPEG, so give `-o` a `.jpg` name or no extension. The script never overwrites a file. Choose `-o` on purpose: where the image belongs in the project, or a scratch folder for drafts. Look at the image before showing it to the user, and pass it back with `-i` to refine it.

## Refreshing the catalog (only when the script asks)

1. Run `catalog`. It prints the path of the user's editable copy, creating it from the bundled one if needed. Edit that file, never the bundled copy.
2. Read https://aistudio.google.com/docs/pricing, https://aistudio.google.com/docs/thinking and https://aistudio.google.com/docs/models.
3. For each model `models` lists, update `input` and `output` (USD per 1M tokens, paid tier), any scheduled `changes`, `effort`, `default_effort` and `use_for`; for image models, `per_image`. Add new model IDs, remove retired ones, and set `verified_on` to today.
4. Run `models` and confirm it shows no warnings. Tell the user what changed.

## Setup problems

- No API key: ask the user to set `GEMINI_API_KEY`, or store the key in the system keychain themselves. On macOS that's `security add-generic-password -a "$USER" -s gemini-api-key -w`; on Linux, `secret-tool store --label="Gemini API key" service gemini-api-key`. Never ask them to paste the key into the chat.
- Defaults live in `~/.config/gemini-skill/config.json`, e.g. `{"default_model": "neon-high", "default_image_model": "nb", "max_input_cost": 0.5}`. The environment variables `GEMINI_DEFAULT_MODEL`, `GEMINI_DEFAULT_IMAGE_MODEL` and `GEMINI_MAX_INPUT_COST` take precedence.
- Learned state (cached model list, accepted and rejected effort levels, alias history) is in `~/.cache/gemini-skill/state.json`. Deleting it is safe.
- `--raw` also prints the full JSON response to stderr, with base64 data shortened.
