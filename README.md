# hype-bot

A personal, non-commercial, **read-only** tool that computes an aggregate, anonymized sentiment and attention
index for a few cryptocurrencies from public crypto subreddits.

## What it does on Reddit
- Every ~10 minutes it reads the newest posts and comments of a small list of public subreddits
  (`SUBREDDITS` in `hype_bot.py`) through the official Reddit Data API (PRAW, app-only, read-only).
- It **never** posts, comments, votes, or sends messages.
- Request volume stays well within Reddit's rate limits (PRAW handles rate limiting).

## What it computes
Per coin and per day: mention volume relative to the coin's own history (`HYPE`), average tone (`YON`),
fear and greed shares, the share of promotional / copy-paste content (`MANIPULASYON`), and a confidence value
based on the number of distinct authors. Only aggregate statistics are produced.

## Data handling and privacy
- Usernames are hashed with a random salt (stored in the OS credential vault) and are never stored in plain text.
- No attempt is made to identify users, de-anonymize data, or infer sensitive characteristics.
- Raw text is kept locally for at most 60 days, then deleted.
- Data is never sold, shared, or used to train machine learning or AI models.
- Optional: text can be labeled for sentiment by a third-party language model API (`--claude`); by default a
  local keyword-based classifier is used.

## Credentials
API credentials are **not** stored in this repository, in environment variables, or in logs. `python hype_bot.py setup`
asks for them with hidden input and stores them in the operating system's credential vault
(Windows Credential Manager / macOS Keychain / Linux Secret Service).

## Usage
```
pip install -r requirements.txt
python hype_bot.py setup            # enter credentials once (hidden input)
python hype_bot.py run --loop 10    # collect + score every 10 minutes
python hype_bot.py score            # latest completed window (read-only database access)
python hype_bot.py status
python hype_bot.py selftest         # offline test, no credentials or network needed
python hype_bot.py forget           # delete stored credentials
```
