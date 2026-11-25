import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from io import BytesIO

import discord
from discord import app_commands

from .api import THMClient, make_session
from .card import render_card
from .models import Room, THMError
from .profile_card import render_profile_card
from .service import Tracker
from .transport import BrowserAPITransport

log = logging.getLogger(__name__)
REQUIRED = ("view_channel", "send_messages", "attach_files", "read_message_history")


def tracking_error_text(message):
    # Previous releases persisted localized errors. Keep stored history intact.
    if re.search(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]", message):
        if "429" in message:
            return "An earlier check returned HTTP 429. A retry is scheduled."
        return "An earlier check failed. A retry is scheduled."
    return message


def require_admin(interaction):
    # Runtime validation; default_permissions alone can be overridden in Discord.
    if not interaction.guild or not interaction.permissions.manage_guild:
        raise THMError("You need the Manage Server permission to use this command.")


def channel_ready(channel, member):
    permissions = channel.permissions_for(member)
    if not all(getattr(permissions, name) for name in REQUIRED):
        raise THMError(
            "The bot needs View Channel, Send Messages, Attach Files, and Read Message History in this channel."
        )


class DiscordSender:
    def __init__(self, bot):
        self.bot = bot

    async def channel(self, guild_id, channel_id):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            raise THMError("The bot is not in this server.")
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise THMError(
                "The achievement channel is unavailable. Run /setup with an accessible text channel."
            )
        if guild.me is None:
            raise THMError("Could not determine the bot's permissions.")
        channel_ready(channel, guild.me)
        return channel

    async def find(self, event, channel_id):
        channel = await self.channel(event["guild_id"], channel_id)
        after = datetime.fromtimestamp(event["created"], UTC) - timedelta(minutes=1)
        async for message in channel.history(limit=200, after=after, oldest_first=False):
            if (
                self.bot.user
                and message.author.id == self.bot.user.id
                and any(a.filename == f"thm-{event['event_id']}.png" for a in message.attachments)
            ):
                return message.id
        return None

    async def send(self, event, room: Room, channel_id, *, test=False, interaction_user=None):
        channel = await self.channel(event["guild_id"], channel_id)
        if interaction_user is not None:
            member = interaction_user
        else:
            try:
                member = channel.guild.get_member(event["user_id"]) or await channel.guild.fetch_member(
                    event["user_id"]
                )
            except discord.NotFound:
                # Leaving the guild ends the binding, including any queued messages.
                self.bot.db.unlink(event["guild_id"], event["user_id"])
                raise THMError("The member left the server. Their local account link was removed.") from None
        avatar = await self.bot.api.image(str(member.display_avatar.url))
        if avatar is None:
            avatar = await self.bot.api.image(event["avatar"])
        room_image = await self.bot.api.image(room.image_url)
        png = await asyncio.to_thread(
            render_card,
            member.display_name,
            event["username"],
            room.title,
            avatar,
            room_image,
            example=test,
            font_path=self.bot.config.font_path,
            difficulty=room.difficulty,
            operating_system=room.operating_system,
        )
        # The card already contains the member, linked username, room, and completion text.
        # Angle brackets keep the room URL clickable without an extra Discord link preview.
        message = await channel.send(
            content=f"<{room.url}>",
            file=discord.File(BytesIO(png), filename=f"thm-{event['event_id']}.png"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return message.id


class AchievementBot(discord.Client):
    def __init__(self, config, db):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config, self.db = config, db
        self.tree = app_commands.CommandTree(self)
        self.worker = None
        self.session = None
        self.api_session = None
        self.api = None
        self.sender = DiscordSender(self)
        self.tracker = None
        self.register_commands()

    async def setup_hook(self):
        self.session = make_session(self.config)
        self.api_session = BrowserAPITransport(self.config)
        self.api = THMClient(self.session, self.config, api_session=self.api_session)
        self.tracker = Tracker(self.db, self.api, self.config, self.sender)
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.worker = asyncio.create_task(self.work(), name="completion-tracker")

    async def work(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.tracker.run()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error("Worker interrupted (%s); restarting in 30s", type(error).__name__)
                await asyncio.sleep(30)

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.session:
            await self.session.close()
        if self.api_session:
            await self.api_session.close()
        await super().close()

    async def on_ready(self):
        log.info("Bot connected; serving %d guilds", len(self.guilds))

    async def on_guild_remove(self, guild):
        async with self.tracker.lock:
            self.db.remove_guild(guild.id)

    def profile_view(self, guild_id, user):
        row = self.db.link(guild_id, user.id)
        view = {"display_name": getattr(user, "display_name", f"Member {user.id}")}
        if row is None:
            pending = self.db.pending(guild_id, user.id)
            valid = pending is not None and pending["expires"] > datetime.now(UTC).timestamp()
            view.update(
                status="VERIFICATION PENDING" if valid else "NOT LINKED",
                warning=True,
                note="Save your Biography code, then use /verify."
                if valid
                else "Use /link to connect your TryHackMe account.",
            )
            return view
        seen, events, waiting = self.db.stats(row["id"])
        checking = self.tracker is not None and row["id"] in getattr(self.tracker, "active_checks", set())
        running = self.tracker is not None and getattr(self.tracker, "running", False)
        note = tracking_error_text(row["last_error"]) if row["last_error"] else ""
        if checking:
            status = "CHECKING NOW"
        elif row["last_error"]:
            status = "RETRY SCHEDULED"
        elif not running:
            status = "TRACKER STARTING / RESTARTING"
            note = "The tracking worker is not active in this instance yet."
        elif not self.db.channel(guild_id):
            status = "CHANNEL NOT CONFIGURED"
            note = "An admin needs to choose an achievement channel with /setup."
        elif row["next_check"] <= datetime.now(UTC).timestamp():
            status = "QUEUED FOR AUTOMATIC CHECK"
        else:
            status = "TRACKING ACTIVE"
        view.update(
            username=row["username"],
            avatar=row["avatar"],
            stats=self.db.profile_data(row["id"]),
            last_success=row["last_success"],
            next_check=row["next_check"],
            checking=checking,
            known=seen,
            announced=events - waiting,
            pending=waiting,
            status=status,
            warning=bool(row["last_error"] or not running or not self.db.channel(guild_id)),
            note=note,
        )
        return view

    async def send_profile_card(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        view = self.profile_view(interaction.guild_id, interaction.user)
        avatar = None
        if self.api is not None:
            display_avatar = getattr(interaction.user, "display_avatar", None)
            if display_avatar is not None:
                avatar = await self.api.image(str(display_avatar.url))
            if avatar is None and view.get("avatar"):
                avatar = await self.api.image(view["avatar"])
        png = await asyncio.to_thread(render_profile_card, view, avatar, font_path=self.config.font_path)
        await interaction.followup.send(
            file=discord.File(BytesIO(png), filename=f"profile-{interaction.user.id}.png"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def register_commands(self):
        tree = self.tree

        @tree.command(name="setup", description="Set the channel for achievement cards")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(channel="Text channel for achievement announcements")
        async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
            require_admin(interaction)
            if channel.guild.id != interaction.guild_id:
                raise THMError("Choose a channel from this server.")
            channel_ready(channel, interaction.guild.me)
            self.db.setup(interaction.guild_id, channel.id)
            await interaction.response.send_message(
                f"Achievement channel set to {channel.mention}.", ephemeral=True
            )

        @tree.command(name="link", description="Start linking your TryHackMe account")
        @app_commands.guild_only()
        @app_commands.describe(username="Your TryHackMe username, without a URL")
        async def link(interaction: discord.Interaction, username: str):
            await interaction.response.defer(ephemeral=True, thinking=True)
            token = await self.tracker.begin(interaction.guild_id, interaction.user.id, username)
            await interaction.followup.send(
                f"Add this entire code to your TryHackMe **Biography (About)**, save your changes, then run `/verify`:\n"
                f"```\n{token}\n```\nValid for {self.config.verify_ttl // 60} minutes. "
                "Your profile must be public. Tracking starts after verification; previous completions will not be announced. "
                "Your linked account, public profile stats, and completed rooms are stored until you use /unlink.",
                ephemeral=True,
            )

        @tree.command(name="verify", description="Verify your Biography code and start tracking")
        @app_commands.guild_only()
        async def verify(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            count = await self.tracker.verify(interaction.guild_id, interaction.user.id)
            await interaction.followup.send(
                f"Account ownership verified. Tracking is now active. Saved {count} existing rooms without announcing them. "
                "You can now remove the code from Biography. Use /unlink to stop tracking and remove your account link.",
                ephemeral=True,
            )

        @tree.command(
            name="unlink", description="Stop tracking and remove your account link from this server"
        )
        @app_commands.guild_only()
        async def unlink(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.tracker.unlink(interaction.guild_id, interaction.user.id)
            await interaction.followup.send(
                "Tracking stopped. Your account link, verification request, and queued announcements were removed from this server.",
                ephemeral=True,
            )

        @tree.command(name="profile", description="Show your TryHackMe profile card")
        @app_commands.guild_only()
        async def profile(interaction: discord.Interaction):
            await self.send_profile_card(interaction)

        @tree.command(name="myprofile", description="Show your private TryHackMe profile image")
        @app_commands.guild_only()
        async def myprofile(interaction: discord.Interaction):
            await self.send_profile_card(interaction)

        @tree.command(name="test", description="Send a clearly labeled test card to the achievement channel")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        async def test(interaction: discord.Interaction):
            require_admin(interaction)
            await interaction.response.defer(ephemeral=True, thinking=True)
            channel_id = self.db.channel(interaction.guild_id)
            if not channel_id:
                raise THMError("Set an achievement channel with /setup first.")
            row = self.db.link(interaction.guild_id, interaction.user.id)
            event = {
                "guild_id": interaction.guild_id,
                "user_id": interaction.user.id,
                "username": row["username"] if row else "EXAMPLE",
                "avatar": "",
                "event_id": "test-" + interaction.id.__str__(),
            }
            # Explicitly synthetic demo. Never inserted into tracking history or API data.
            await self.sender.send(
                event,
                Room(
                    "internal",
                    "Internal",
                    "https://cdn-images.tryhackme.com/room-icons/222b3e855f88a482c1267748f76f90e0.jpeg",
                    "hard",
                    ("Linux",),
                ),
                channel_id,
                test=True,
                interaction_user=interaction.user,
            )
            await interaction.followup.send("Test card sent. No achievement was recorded.", ephemeral=True)

        @tree.error
        async def command_error(interaction, error):
            original = getattr(error, "original", error)
            if isinstance(original, THMError):
                message = str(original)
            elif isinstance(original, app_commands.NoPrivateMessage):
                message = "This command can only be used in a server."
            elif isinstance(original, discord.Forbidden):
                message = "The bot is missing required channel permissions. Check /setup and the README."
            else:
                log.error("Command failed (%s)", type(original).__name__)
                message = "Could not complete the command. Try again later and check the API, channel, and local logs."
            if interaction.response.is_done():
                await interaction.followup.send(
                    message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                await interaction.response.send_message(
                    message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
                )
