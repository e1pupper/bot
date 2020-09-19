import base64
import binascii
import logging
import re
import typing as t

from discord import Colour, Message, NotFound
from discord.ext.commands import Cog

from bot import utils
from bot.bot import Bot
from bot.cogs.moderation import ModLog
from bot.constants import Channels, Colours, Event, Icons

log = logging.getLogger(__name__)

LOG_MESSAGE = (
    "Censored a seemingly valid token sent by {author} (`{author_id}`) in {channel}, "
    "token was `{user_id}.{timestamp}.{hmac}`"
)
DECODED_LOG_MESSAGE = "The token user_id decodes into {user_id}."
USER_TOKEN_MESSAGE = (
    "The token user_id decodes into {user_id}, "
    "which matches `{user_name}` and means this is a valid USER token."
)
DELETION_MESSAGE_TEMPLATE = (
    "Hey {mention}! I noticed you posted a seemingly valid Discord API "
    "token in your message and have removed your message. "
    "This means that your token has been **compromised**. "
    "Please change your token **immediately** at: "
    "<https://discordapp.com/developers/applications/me>\n\n"
    "Feel free to re-post it with the token removed. "
    "If you believe this was a mistake, please let us know!"
)
DISCORD_EPOCH = 1_420_070_400
TOKEN_EPOCH = 1_293_840_000

# Three parts delimited by dots: user ID, creation timestamp, HMAC.
# The HMAC isn't parsed further, but it's in the regex to ensure it at least exists in the string.
# Each part only matches base64 URL-safe characters.
# Padding has never been observed, but the padding character '=' is matched just in case.
TOKEN_RE = re.compile(r"([\w\-=]+)\.([\w\-=]+)\.([\w\-=]+)", re.ASCII)


class Token(t.NamedTuple):
    """A Discord Bot token."""

    user_id: str
    timestamp: str
    hmac: str


class TokenRemover(Cog):
    """Scans messages for potential discord.py bot tokens and removes them."""

    def __init__(self, bot: Bot):
        self.bot = bot

    @property
    def mod_log(self) -> ModLog:
        """Get currently loaded ModLog cog instance."""
        return self.bot.get_cog("ModLog")

    @Cog.listener()
    async def on_message(self, msg: Message) -> None:
        """
        Check each message for a string that matches Discord's token pattern.

        See: https://discordapp.com/developers/docs/reference#snowflakes
        """
        # Ignore DMs; can't delete messages in there anyway.
        if not msg.guild or msg.author.bot:
            return

        found_token = self.find_token_in_message(msg)
        if found_token:
            await self.take_action(msg, found_token)

    @Cog.listener()
    async def on_message_edit(self, before: Message, after: Message) -> None:
        """
        Check each edit for a string that matches Discord's token pattern.

        See: https://discordapp.com/developers/docs/reference#snowflakes
        """
        await self.on_message(after)

    async def take_action(self, msg: Message, found_token: Token) -> None:
        """Remove the `msg` containing the `found_token` and send a mod log message."""
        self.mod_log.ignore(Event.message_delete, msg.id)

        try:
            await msg.delete()
        except NotFound:
            log.debug(f"Failed to remove token in message {msg.id}: message already deleted.")
            return

        await msg.channel.send(DELETION_MESSAGE_TEMPLATE.format(mention=msg.author.mention))

        user_name = None
        user_id = self.extract_user_id(found_token.user_id)
        user = msg.guild.get_member(user_id)

        if user:
            user_name = str(user)

        log_message = self.format_log_message(msg, found_token, user_id, user_name)
        log.debug(log_message)

        # Send pretty mod log embed to mod-alerts
        await self.mod_log.send_log_message(
            icon_url=Icons.token_removed,
            colour=Colour(Colours.soft_red),
            title="Token removed!",
            text=log_message,
            thumbnail=msg.author.avatar_url_as(static_format="png"),
            channel_id=Channels.mod_alerts,
            ping_everyone=user_name is not None,
        )

        self.bot.stats.incr("tokens.removed_tokens")

    @staticmethod
    def format_log_message(
        msg: Message,
        token: Token,
        user_id: int,
        user_name: t.Optional[str] = None,
    ) -> str:
        """
        Return the log message to send for `token` being censored in `msg`.

        Additonally, mention if the token was decodable into a user id, and if that resolves to a user on the server.
        """
        message = LOG_MESSAGE.format(
            author=msg.author,
            author_id=msg.author.id,
            channel=msg.channel.mention,
            user_id=token.user_id,
            timestamp=token.timestamp,
            hmac='x' * len(token.hmac),
        )
        if user_name:
            more = USER_TOKEN_MESSAGE.format(user_id=user_id, user_name=user_name)
        else:
            more = DECODED_LOG_MESSAGE.format(user_id=user_id)
        return message + "\n" + more

    @classmethod
    def find_token_in_message(cls, msg: Message) -> t.Optional[Token]:
        """Return a seemingly valid token found in `msg` or `None` if no token is found."""
        # Use finditer rather than search to guard against method calls prematurely returning the
        # token check (e.g. `message.channel.send` also matches our token pattern)
        for match in TOKEN_RE.finditer(msg.content):
            token = Token(*match.groups())
            if cls.is_valid_user_id(token.user_id) and cls.is_valid_timestamp(token.timestamp):
                # Short-circuit on first match
                return token

        # No matching substring
        return

    @staticmethod
    def extract_user_id(b64_content: str) -> t.Optional[int]:
        """Return a userid integer from part of a potential token, or None if it couldn't be decoded."""
        b64_content = utils.pad_base64(b64_content)

        try:
            decoded_bytes = base64.urlsafe_b64decode(b64_content)
            string = decoded_bytes.decode('utf-8')
            if not (string.isascii() and string.isdigit()):
                # This case triggers if there are fancy unicode digits in the base64 encoding,
                # that means it's not a valid user id.
                return None
            return int(string)
        except (binascii.Error, ValueError):
            return None

    @classmethod
    def is_valid_user_id(cls, b64_content: str) -> bool:
        """
        Check potential token to see if it contains a valid Discord user ID.

        See: https://discordapp.com/developers/docs/reference#snowflakes
        """
        decoded_id = cls.extract_user_id(b64_content)
        if not decoded_id:
            return False

        return True

    @staticmethod
    def is_valid_timestamp(b64_content: str) -> bool:
        """
        Return True if `b64_content` decodes to a valid timestamp.

        If the timestamp is greater than the Discord epoch, it's probably valid.
        See: https://i.imgur.com/7WdehGn.png
        """
        b64_content = utils.pad_base64(b64_content)

        try:
            decoded_bytes = base64.urlsafe_b64decode(b64_content)
            timestamp = int.from_bytes(decoded_bytes, byteorder="big")
        except (binascii.Error, ValueError) as e:
            log.debug(f"Failed to decode token timestamp '{b64_content}': {e}")
            return False

        # Seems like newer tokens don't need the epoch added, but add anyway since an upper bound
        # is not checked.
        if timestamp + TOKEN_EPOCH >= DISCORD_EPOCH:
            return True
        else:
            log.debug(f"Invalid token timestamp '{b64_content}': smaller than Discord epoch")
            return False


def setup(bot: Bot) -> None:
    """Load the TokenRemover cog."""
    bot.add_cog(TokenRemover(bot))
