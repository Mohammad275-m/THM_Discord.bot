"""Offline design preview; explicitly illustrative, never an API substitute."""

from thm_bot.card import render_card
from thm_bot.config import ROOT


def main():
    target = ROOT / "examples" / "card-example.png"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(
        render_card(
            "Example Discord Member", "EXAMPLE_ACCOUNT", "Example room — Network Fundamentals", example=True
        )
    )
    print(f"Local illustrative preview (NOT real API data): {target}")


if __name__ == "__main__":
    main()
