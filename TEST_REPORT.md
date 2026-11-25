# Test report

This repository is validated locally with synthetic fixtures and mocked Discord
delivery. The test suite does not require a Discord token and does not send
Discord messages.

## Current validation

- 102 tests passed
- Ruff checks passed
- Python bytecode compilation passed
- ZIP contents were checked for path traversal entries
- No credentials or `.env` file are included

Live TryHackMe diagnostics are optional. Run them only with an explicit public
username and respect TryHackMe's rate limits:

```bash
python -m scripts.probe --username YOUR_USERNAME
```

Live API behavior and Discord delivery are not guaranteed by this local test
report. The public TryHackMe endpoints used by this project are unofficial and
may change.
