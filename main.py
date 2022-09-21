import asyncio
import datetime
import logging
import random
import signal
import time
from copy import deepcopy
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from naff import (
    Client,
    Intents,
    listen,
    MISSING,
    ComponentContext,
    Snowflake_Type,
    IntervalTrigger,
    Task,
    Modal,
    ShortText,
    CommandTypes,
    InteractionContext,
    ThreadChannel,
    Status,
    BaseChannel,
    BrandColors,
    Embed,
)
from naff.api.events import Button, MessageReactionAdd, ModalResponse, GuildLeft
from naff.client.errors import NotFound
from naff.models.naff.application_commands import context_menu, slash_command

from models.poll import PollData, sanity_check
from poll_cache import PollCache

__all__ = ("Bot",)

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("Inquiry")

ap_log = logging.getLogger("apscheduler")
ap_log.setLevel(logging.WARNING)


class Bot(Client):
    def __init__(self) -> None:
        super().__init__(
            intents=Intents.new(guilds=True, reactions=True, default=False),
            sync_interactions=True,
            delete_unused_application_cmds=False,
            activity="with an update...",
            status=Status.DND,
        )
        self.poll_cache: PollCache = MISSING

        self.polls_to_update: dict[Snowflake_Type, set[Snowflake_Type]] = {}

        self.update_lock = asyncio.Lock()  # prevent concurrent updates

        self.scheduler = AsyncIOScheduler()

    @classmethod
    async def run(cls, token: str) -> None:
        bot = cls()

        signal.signal(signal.SIGINT, lambda *_: asyncio.create_task(bot.stop()))
        signal.signal(signal.SIGTERM, lambda *_: asyncio.create_task(bot.stop()))

        bot.load_extension("extensions.create_poll")
        bot.load_extension("extensions.edit_poll")
        bot.load_extension("extensions.poll_utils")
        bot.load_extension("extensions.admin")
        bot.load_extension("extensions.bot_lists")
        bot.load_extension("extensions.help")
        bot.load_extension("extensions.analytics")
        bot.load_extension("extensions.dev")

        for command in bot.application_commands:
            # it really isnt necessary to do it like this, but im really lazy
            # basically this disables using every command in dms **except** the commands in this file
            command.dm_permission = False

        bot.poll_cache = await PollCache.initialize(bot)

        bot.__update_polls.start()
        bot.scheduler.start()

        await bot.astart(token)

    async def set_poll(self, poll: PollData) -> None:
        await self.poll_cache.store_poll(poll)

    @listen()
    async def on_startup(self) -> Any:
        await self.poll_cache.ready.wait()
        log.info(f"Logged in as {self.user.username}")
        log.info(f"Currently in {len(self.guilds)} guilds")
        await self.change_presence(activity="with polls", status=Status.ONLINE)

    @slash_command("invite", description="Get the invite link for this bot")
    async def invite(self, ctx: InteractionContext):
        await ctx.send(
            f"https://discord.com/api/oauth2/authorize?client_id={self.app.id}&permissions=377957124096&scope=bot%20applications.commands"
        )

    @slash_command("server", description="Join the support server")
    async def server(self, ctx: InteractionContext) -> None:
        await ctx.send("https://discord.gg/vtRTAwmQsH")

    @slash_command("feedback", description="Send feedback to the bot owner")
    async def feedback(self, ctx: InteractionContext):
        await ctx.send("Thank you!\nhttps://forms.gle/6NDMJQXqmWL8fQVm6")

    @listen()
    async def on_modal_response(self, event: ModalResponse) -> Any:
        ctx = event.context
        ids = ctx.custom_id.split("|")
        if len(ids) == 2:
            await ctx.defer(ephemeral=True)
            if not await sanity_check(ctx):
                return

            message_id = ctx.custom_id.split("|")[1]
            if poll := await self.poll_cache.get_poll(message_id):
                async with poll.lock:
                    poll.add_option(ctx.responses["new_option"])

                    if ctx.guild.id not in self.polls_to_update:
                        self.polls_to_update[ctx.guild.id] = set()
                    self.polls_to_update[ctx.guild.id].add(int(message_id))
                return await ctx.send(f"Added {ctx.responses['new_option']} to the poll")
            return await ctx.send("That poll could not be edited")

    @listen()
    async def on_button(self, event: Button) -> Any:
        ctx: ComponentContext = event.context
        if isinstance(ctx.channel, ThreadChannel):
            message_id = ctx.channel.id
        else:
            message_id = ctx.message.id

        if ctx.custom_id == "add_option":
            if await self.poll_cache.get_poll(message_id):
                return await ctx.send_modal(
                    Modal(
                        "Add Option",
                        [ShortText(label="Option", custom_id="new_option")],
                        custom_id="add_option_modal|{}".format(ctx.message.id),
                    )
                )
            else:
                return await ctx.send("Cannot add options to that poll", ephemeral=True)
        elif "poll_option" in ctx.custom_id:
            await ctx.defer(ephemeral=True)

            option_index = int(ctx.custom_id.removeprefix("poll_option|"))

            if poll := await self.poll_cache.get_poll(message_id):
                if poll.expired:
                    message = await self.cache.fetch_message(ctx.channel.id, message_id)
                    await message.edit(components=poll.get_components(disable=True))
                    return await ctx.send("This poll is closing - your vote will not be counted", ephemeral=True)
                async with poll.lock:
                    if not poll.expired:
                        opt = poll.poll_options[option_index]
                        if poll.single_vote:
                            for _o in poll.poll_options:
                                if _o != opt:
                                    if ctx.author.id in _o.voters:
                                        _o.voters.remove(ctx.author.id)
                        if opt.vote(ctx.author.id):
                            await ctx.send(f"⬆️ Your vote for {opt.emoji}`{opt.inline_text}` has been added!")
                        else:
                            await ctx.send(f"⬇️ Your vote for {opt.emoji}`{opt.inline_text}` has been removed!")

                    if ctx.guild.id not in self.polls_to_update:
                        self.polls_to_update[ctx.guild.id] = set()
                    self.polls_to_update[ctx.guild.id].add(poll.message_id)
            else:
                await ctx.send("That poll could not be edited 😕")

    @listen()
    async def on_message_reaction_add(self, event: MessageReactionAdd) -> None:
        if event.emoji.name in ("🔴", "🛑", "🚫", "⛔"):
            poll = await self.poll_cache.get_poll(event.message.id)
            if poll:
                if event.author.id == poll.author_id:
                    await self.close_poll(poll.message_id)

    @Task.create(IntervalTrigger(seconds=5))
    async def __update_polls(self) -> None:
        # messages edits have a 5-second rate limit, while technically you can edit a message multiple times within those 5 seconds
        # its a better idea to just over compensate and only edit once per 5 seconds
        tasks = []
        async with self.update_lock:
            polls = deepcopy(self.polls_to_update)

            if polls:
                for guild in polls:
                    for message_id in polls[guild]:
                        try:
                            poll = await self.poll_cache.get_poll(message_id)
                            if not poll.expired:
                                try:
                                    await self.cache.fetch_message(poll.channel_id, poll.message_id)
                                except NotFound:
                                    log.warning(f"Poll {poll.message_id} not found - deleting from cache")
                                    await self.poll_cache.delete_poll(poll.channel_id, poll.message_id)
                                    self.polls_to_update[guild].remove(message_id)
                                    continue
                                else:
                                    tasks.append(poll.update_messages(self))
                                    tasks.append(self.poll_cache.store_poll(poll))

                                finally:
                                    self.polls_to_update[guild].remove(message_id)
                                log.debug(f"Updated poll {poll.message_id}")
                        except Exception as e:
                            log.error(f"Error updating poll {message_id}", exc_info=e)
            await asyncio.gather(*tasks)

    async def schedule_close(self, poll: PollData) -> None:
        if poll.expire_time and not poll.closed:
            try:
                self.scheduler.reschedule_job(job_id=str(poll.message_id), trigger=DateTrigger(poll.expire_time))
                log.info(f"Rescheduled poll {poll.message_id} to close at {poll.expire_time}")
            except JobLookupError:
                if poll.expire_time > datetime.datetime.now():
                    self.scheduler.add_job(
                        id=str(poll.message_id),
                        name=f"Close Poll {poll.message_id}",
                        trigger=DateTrigger(poll.expire_time),
                        func=self.close_poll,
                        args=[poll.message_id],
                    )
                    log.info(f"Scheduled poll {poll.message_id} to close at {poll.expire_time}")
                else:
                    await self.close_poll(poll.message_id)
                    log.info(f"Poll {poll.message_id} already expired - closing immediately")

    async def close_poll(self, message_id):
        poll = await self.poll_cache.get_poll(message_id)
        tasks = []
        if poll:
            async with poll.lock:
                log.info(f"Closing poll {poll.message_id}")
                poll._expired = True
                poll.expire_time = datetime.datetime.now()

                tasks.append(poll.update_messages(self))
                tasks.append(poll.send_close_message(self))
                poll.closed = True

                tasks.append(self.poll_cache.store_poll(poll))
                tasks.append(self.send_thanks_message(poll.channel_id))
        else:
            log.warning(f"Poll {message_id} not found - cannot close")

        try:
            await asyncio.gather(*tasks)
        except NotFound:
            log.warning(f"Poll {message_id} is no longer on discord - deleting from database")
            await self.poll_cache.delete_poll(poll.message_id)
        except Exception as e:
            log.error(f"Error closing poll {message_id}", exc_info=e)

    @context_menu("stress poll", CommandTypes.MESSAGE, scopes=[985991455074050078])
    async def __stress_poll(self, ctx: ComponentContext) -> None:
        # stresses out the poll system by voting a huge amount on a poll
        # this is a stress test for the system, and should not be used in production
        poll = await self.poll_cache.get_poll(ctx.target.id)
        votes_per_cycle = 30000
        cycles = 10

        if poll:
            msg = await ctx.send("Stress testing...")

            for i in range(cycles):
                start = time.perf_counter()
                for _ in range(votes_per_cycle):
                    async with poll.lock:
                        opt = random.choice(poll.poll_options)
                        voter = random.randrange(1, 10**11)
                        if not poll.expired:
                            if poll.single_vote:
                                for _o in poll.poll_options:
                                    if _o != opt:
                                        if voter in _o.voters:
                                            _o.voters.remove(voter)
                            opt.vote(voter)
                            if ctx.guild.id not in self.polls_to_update:
                                self.polls_to_update[ctx.guild.id] = set()
                            self.polls_to_update[ctx.guild.id].add(poll.message_id)

                end = time.perf_counter()

                await asyncio.sleep(2 - (end - start))
                await msg.edit(
                    content=f"Stress testing... {i+1}/{cycles} ({votes_per_cycle:,} votes per cycle) @ {round(votes_per_cycle / (end - start)):,} votes per second"
                )
            await msg.edit(
                content=f"Stress Completed... {i+1}/{cycles} ({votes_per_cycle:,} votes per cycle) @ {round(votes_per_cycle / (end - start)):,} votes per second"
            )

        else:
            await ctx.send("That poll could not be found")

    async def send_thanks_message(self, channel_id: Snowflake_Type) -> None:
        try:
            channel = await self.cache.fetch_channel(channel_id)
            if channel:
                total_polls = await self.poll_cache.db.fetchval(
                    "SELECT COUNT(*) FROM polls.poll_data WHERE guild_id = $1", channel.guild.id
                )
                if total_polls == 2:
                    embed = Embed(title="Thanks for using Inquiry!", color=BrandColors.BLURPLE)
                    embed.description = f"If you have any questions try {self.server.mention()} \nIf you have feedback use {self.feedback.mention()}. \n\nOtherwise, enjoy the bot!"
                    embed.set_footer(
                        text="This is the only time Inquiry will send a message like this",
                        icon_url=self.user.avatar.url,
                    )
                    await channel.send(embed=embed)
                    log.info(f"Sent thanks message to {channel.guild.id}")
        except Exception as e:
            log.error("Error sending thanks message", exc_info=e)

    @listen()
    async def on_guild_remove(self, event: GuildLeft) -> None:
        if self.is_ready:
            try:
                total_polls = await self.poll_cache.db.fetchval(
                    "SELECT COUNT(*) FROM polls.poll_data WHERE guild_id = $1", event.guild.id
                )

                await self.poll_cache.db.execute("DELETE FROM polls.poll_data WHERE guild_id = $1", event.guild.id)
                log.info(f"Left guild {event.guild.id} -- Deleted {total_polls} related polls from database")
            except Exception as e:
                log.error("Error deleting polls on guild leave", exc_info=e)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv()
    token = os.getenv("TOKEN")
    asyncio.run(Bot.run(token))
