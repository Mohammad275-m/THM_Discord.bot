# THM Discord Bot

An opt-in Discord bot that tracks public TryHackMe room completions and posts
simple achievement cards.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Set `DISCORD_TOKEN` in `.env`, then run:

```bash
.venv/bin/python -m thm_bot
```

Use `/setup`, `/link`, and `/verify` in Discord.

## Security

Never commit `.env`, bot tokens, or the `data/` directory. This project uses
only public TryHackMe profile data and does not request TryHackMe passwords.

## License

MIT. See [LICENSE](LICENSE).
