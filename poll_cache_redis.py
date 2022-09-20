import asyncio
import logging
from asyncio import Event
from contextlib import suppress

import aioredis
import orjson
from naff import Snowflake_Type
from naff.client.errors import Forbidden

from models.poll import PollData

log = logging.getLogger("Cache")


class PollCache:
    def __init__(self, bot, redis: aioredis.Redis):
        self.bot = bot
        self.redis: aioredis.Redis = redis

        # poll caches / lookups
        self.polls: list[PollData] = list()
        """A set of polls that are currently cached"""
        self.polls_by_guild: dict[Snowflake_Type, set[Snowflake_Type]] = {}
        """A dictionary of guild_id -> set of message_id"""
        self.polls_by_message: dict[Snowflake_Type, PollData] = {}
        """A dictionary of message_id -> poll"""
        self.ready: Event = Event()

    @classmethod
    async def initialize(cls, bot):
        try:
            try:
                redis = await aioredis.from_url("redis://redis/5", decode_responses=True)
                await redis.ping()
            except Exception as e:
                redis = await aioredis.from_url("redis://localhost/5", decode_responses=True)
                await redis.ping()
            instance = cls(bot, redis)
            asyncio.create_task(instance.load_all_from_redis())
            return instance
        except Exception as e:
            log.critical(f"Failed to initialize cache", exc_info=e)
            raise e

    @property
    def total_polls(self) -> int:
        return len(self.polls)

    @staticmethod
    def _to_optional_snowflake(value) -> int | None:
        if value == "None":
            # handle deserialization
            return None
        return int(value)

    async def __fetch_poll(self, key: str) -> PollData | None:
        try:
            raw_data = await self.redis.get(key)

            data = orjson.loads(raw_data)

            if author_data := data.pop("author_data", None):
                data["author_name"] = author_data["name"]
                data["author_avatar"] = author_data["avatar_url"]

            poll = PollData(**data)
            guild_id, msg_id = [self._to_optional_snowflake(k) for k in key.split("|")]
            if guild_id is None:
                # broken poll, delete
                # these were created in an earlier version of the bot
                log.warning("Deleting broken poll: %s", key)
                await self.redis.delete(key)
                return None

            if not poll.author_name or not poll.author_avatar or poll.author_name == "Unknown":
                try:
                    author = await self.bot.fetch_member(poll.author_id, guild_id)
                except Forbidden:
                    author = None
                if author:
                    poll.author_name = author.display_name
                    poll.author_avatar = author.avatar_url
                else:
                    poll.author_name = "Unknown"
                    poll.author_avatar = "https://cdn.discordapp.com/embed/avatars/0.png"

            # legacy poll support
            if not poll.guild_id:
                poll.guild_id = guild_id
            if not poll.message_id:
                poll.message_id = msg_id

            self.polls.append(poll)
            if guild_id not in self.polls_by_guild:
                self.polls_by_guild[guild_id] = set()
            self.polls_by_guild[guild_id].add(msg_id)
            self.polls_by_message[msg_id] = poll
            log.debug("Cached poll: %s", key)

            return poll
        except (ValueError, KeyError, TypeError) as e:
            log.warning(f"Failed to fetch poll: {key}", exc_info=e)

    async def load_all_from_redis(self):
        if not self.bot.is_ready:
            log.debug("Waiting for client to be ready")
            await self.bot.wait_until_ready()
        log.info("Loading polls from redis...")
        await asyncio.gather(*[self.__fetch_poll(k) for k in await self.redis.keys("*")])
        log.info(f"Loaded {self.total_polls} polls")
        self.ready.set()

    async def get_poll_by_message(self, message_id: Snowflake_Type) -> PollData:
        return self.polls_by_message.get(message_id)

    async def get_poll(self, guild_id: Snowflake_Type, message_id: Snowflake_Type) -> PollData:
        if message_id in self.polls_by_message:
            return self.polls_by_message[message_id]
        log.warning(f"Poll {message_id} not found in cache")
        return await self.__fetch_poll(f"{guild_id}|{message_id}")

    async def get_polls_by_guild(self, guild_id: Snowflake_Type) -> list[PollData]:
        message_ids = self.polls_by_guild.get(guild_id, set())
        polls = [self.polls_by_message.get(msg_id, None) for msg_id in message_ids]
        return list(filter(None, polls))

    async def store_poll(self, guild_id: Snowflake_Type, message_id: Snowflake_Type, poll: PollData) -> None:
        async with poll.lock:
            key = f"{guild_id}|{message_id}"
            if key is None or message_id is None:
                raise ValueError("Invalid message_id or guild_id")

            if poll not in self.polls:
                self.polls.append(poll)
            if not guild_id in self.polls_by_guild:
                self.polls_by_guild[guild_id] = set()
            self.polls_by_guild[guild_id].add(message_id)
            self.polls_by_message[message_id] = poll

            serialised = orjson.dumps(poll.__dict__())
            await self.redis.set(key, serialised)
            log.debug("Stored poll: %s", key)

    async def delete_poll(self, guild_id, message_id):
        key = f"{guild_id}|{message_id}"
        await self.redis.delete(key)

        with suppress(KeyError, ValueError):
            self.polls.remove(self.polls_by_message[message_id])
        with suppress(KeyError):
            self.polls_by_guild[guild_id].remove(message_id)
        with suppress(KeyError):
            del self.polls_by_message[message_id]

        log.debug("Deleted poll: %s", key)
