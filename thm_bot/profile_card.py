"""Private profile PNGs built from the last complete cached API snapshot."""

from datetime import UTC, datetime
from io import BytesIO

from PIL import Image, ImageDraw

from .card import GREEN, MUTED, NAVY, PANEL, WHITE, clean, fit, font, picture


def format_stat(value):
    return f"{value:,}" if type(value) is int else "—"


def check_label(next_check, *, now, checking=False, linked=True):
    if not linked:
        return "Not scheduled"
    if checking:
        return "Checking now"
    if next_check is None or next_check <= now:
        return "Queued for automatic check"
    remaining = max(0, int(next_check - now))
    return f"In {remaining // 60}m {remaining % 60:02d}s (as of this image)"


def render_profile_card(view, avatar_data=None, *, font_path="", now=None):
    now = datetime.now(UTC).timestamp() if now is None else now
    image = Image.new("RGB", (1400, 760), NAVY)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, 1387, 747), radius=26, outline="#263958", width=2)
    draw.ellipse((44, 36, 57, 49), fill=GREEN)
    draw.text((70, 27), "TRYHACKME / MY PROFILE", font=font(20), fill=MUTED)
    draw.text(
        (1060, 30), datetime.fromtimestamp(now, UTC).strftime("AS OF %H:%M UTC"), font=font(17), fill=MUTED
    )
    draw.line((40, 78, 1360, 78), fill="#263958")

    avatar = picture(avatar_data, (244, 280), "MEMBER")
    mask = Image.new("L", avatar.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 243, 279), radius=18, fill=255)
    image.paste(avatar, (44, 108), mask)
    draw.rounded_rectangle((44, 108, 288, 388), radius=18, outline=GREEN, width=3)
    name, selected = fit(draw, clean(view.get("display_name", "Discord member")), 990, 45, 28, font_path)
    draw.text((330, 104), name, font=selected, fill=WHITE)
    username = view.get("username")
    account, selected = fit(draw, "TryHackMe: " + (username or "Not linked"), 990, 25, 18, font_path)
    draw.text((332, 168), account, font=selected, fill=GREEN)
    status = clean(view.get("status", "NOT LINKED"))
    status, selected = fit(draw, status, 990, 20, 16, font_path)
    draw.text((332, 213), status, font=selected, fill="#ffca78" if view.get("warning") else GREEN)

    stats = view.get("stats", {})
    metrics = [
        ("COMPLETED ROOMS", stats.get("completed_count")),
        ("TOTAL POINTS", stats.get("total_points")),
        ("GLOBAL RANK", stats.get("rank")),
        ("CURRENT STREAK", stats.get("streak")),
        ("LEVEL", stats.get("level")),
        ("BADGES", stats.get("badges_count")),
    ]
    for index, (label, value) in enumerate(metrics):
        x = 332 + (index % 3) * 346
        y = 266 + (index // 3) * 118
        draw.rounded_rectangle((x, y, x + 322, y + 100), radius=14, fill=PANEL)
        draw.text((x + 18, y + 14), label, font=font(17), fill=MUTED)
        rendered = format_stat(value)
        if label == "CURRENT STREAK" and type(value) is int:
            rendered += " day" if value == 1 else " days"
        rendered, selected = fit(draw, rendered, 286, 38, 24, font_path)
        draw.text((x + 18, y + 41), rendered, font=selected, fill=WHITE)

    draw.rounded_rectangle((44, 536, 1356, 638), radius=15, fill=PANEL)
    draw.text((64, 551), "LAST SUCCESSFUL CHECK", font=font(16), fill=MUTED)
    last_success = view.get("last_success")
    last = (
        datetime.fromtimestamp(last_success, UTC).strftime("%d %b %Y · %H:%M:%S UTC")
        if last_success
        else "No completed check yet"
    )
    draw.text((64, 585), last, font=font(21), fill=WHITE)
    draw.text((722, 551), "NEXT CHECK", font=font(16), fill=MUTED)
    next_label = check_label(
        view.get("next_check"), now=now, checking=view.get("checking", False), linked=bool(username)
    )
    next_label, selected = fit(draw, next_label, 610, 21, 16, font_path)
    draw.text((722, 585), next_label, font=selected, fill=WHITE)

    summary = (
        f"ANNOUNCEMENTS SENT  {view.get('announced', 0)}     ·     "
        f"PENDING  {view.get('pending', 0)}     ·     KNOWN ROOM IDS  {view.get('known', 0)}"
    )
    summary, selected = fit(draw, summary, 1280, 19, 15, font_path)
    draw.text((48, 665), summary, font=selected, fill=MUTED)
    note = view.get("note") or (
        f"tryhackme.com/p/{username}" if username else "Use /link to connect your TryHackMe account."
    )
    note, selected = fit(draw, clean(note), 1280, 17, 14, font_path)
    draw.text((48, 711), note, font=selected, fill=MUTED)
    buffer = BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()
