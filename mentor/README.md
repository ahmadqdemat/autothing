# Mentor - Home Assistant App

Mentor is an AI tutoring assistant powered by Google Gemini. It guides school students through homework step by step without giving away the answers.

## Features
- Hint ladder: clarify, concept, strategy, nudge, check. Resets when a new problem starts.
- Photos of worksheets: camera button or file picker. Photos are shrunk before upload.
- Voice input in Arabic or English (needs HTTPS, for example through the Home Assistant app or Nabu Casa).
- Answer check: one extra, small Gemini call verifies that a hint doesn't reveal the answer.
- Full mode for adults, unlocked with the passphrase; it relocks after the idle timeout.
- Parent view (sidebar, same passphrase): all conversations, message counts and how often the answer check stepped in.

## Configuration
- gemini_api_key: your Google Gemini API key.
- model: main model, for example gemini-2.5-flash.
- thinking_level: how long the model thinks before a tutoring reply (low, medium or high). Low is fastest. Empty uses the model default.
- leak_check_model: optional faster model for the answer check, for example a flash-lite model. Empty uses the main model.
- unlock_passphrase: passphrase for full mode and the parent view.
- unlock_idle_timeout_minutes: full mode relocks after this many idle minutes.
- max_unlock_attempts / lockout_minutes: wrong passphrases allowed before unlocking is paused (counted for the whole add-on).
- leak_check_enabled: turn the answer check on or off.
- log_conversations: save conversations and photos to disk. When off, they are kept in memory only.

## Unlock and relock
- Chat: send /unlock followed by the passphrase, or /lock. These commands are handled by the server and never sent to Gemini or saved.
- Button: Unlock in the top bar.

## Development
pip install -r requirements-dev.txt, then run pytest from this folder.
