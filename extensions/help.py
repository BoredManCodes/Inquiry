import logging

import attrs
import yaml
from naff import (
    Extension,
    slash_command,
    InteractionContext,
    AutocompleteContext,
    slash_option,
    Embed,
    BrandColours,
    File,
    listen,
)
from prometheus_client import Gauge
from thefuzz import process


@attrs.define
class HelpTopic:
    """An object representing a help topic and its data"""

    title: str = attrs.field()
    brief: str = attrs.field()
    emoji: str = attrs.field()

    content: str = attrs.field()
    image: str | None = attrs.field(default=None)


class HelpExtension(Extension):
    def __init__(self, *args, **kwargs):
        self.topics: dict[str, HelpTopic] = {}

        with open("data/help.yml", encoding="UTF-8") as f:
            data = yaml.safe_load(f)
            for topic, value in data.items():
                self.topics[topic] = HelpTopic(**value)

        self.first_poll_analytics = Gauge("inquiry_first_poll", "How users have created their first poll")

    @slash_command("help", description="Get help with the bot")
    @slash_option("topic", description="The topic to get help with", opt_type=3, required=False)
    async def help_main(self, ctx: InteractionContext, topic: str | None = None) -> None:
        if topic:
            if topic in self.topics:
                _t = self.topics[topic]
                embed = Embed(title=f"{_t.emoji} {_t.title}", description=_t.content, color=BrandColours.BLURPLE)
                if _t.image:
                    embed.set_image(url=f"attachment://{_t.image}")
                await ctx.send(
                    embed=embed, file=File(f"resources/{_t.image}", file_name=_t.image) if _t.image else None
                )

            else:
                await ctx.send(f"Unable to find the requested help topic: {topic}")
        else:
            embed = Embed(
                title="Help Topics",
                description="Use `/help <topic>` to get help with a specific topic.\n\n",
                color=BrandColours.BLURPLE,
            )
            sorted_topics = sorted(self.topics.values(), key=lambda t: t.title)

            for topic in sorted_topics:
                embed.add_field(name=f"{topic.emoji} {topic.title}", value=topic.brief, inline=False)

            embed.set_footer(
                text="Think we need a new help topic? Let us know! Use `/feedback` to send us feedback.",
                icon_url=self.bot.user.avatar.url,
            )

            await ctx.send(embed=embed)

    @help_main.autocomplete("topic")
    async def help_topic_auto_complete(self, ctx: AutocompleteContext, **kwargs) -> None:
        """Autocomplete for the help topic"""
        if not ctx.input_text:
            results = list(self.topics.keys())[:25]
        else:
            results = process.extract(ctx.input_text, self.topics.keys(), limit=25)
            results = [r[0] for r in results if r[1] > 50]

        await ctx.send(results)

    @listen("on_poll_create")
    async def first_poll(self, event):
        poll = event.poll
        user_polls = await self.bot.poll_cache.db.fetchval(
            "SELECT COUNT(*) FROM polls.poll_data WHERE author_id = $1", poll.author_id
        )
        if user_polls <= 1:
            logging.info("First poll created by %s", poll.author_id)
            self.first_poll_analytics.inc()

            export_command = self.bot.interactions[0].get("export text")
            help_command = self.bot.interactions[0].get("help")
            server_command = self.bot.interactions[0].get("server")
            feedback_command = self.bot.interactions[0].get("feedback")

            embed = Embed("🎉 Congratulations! 🎉", color=BrandColours.GREEN)
            embed.description = "It looks like you have created your first poll!\nHere are some things you can do next:"
            embed.add_field("📊 View Who's Voted", f"with {export_command.mention()}")
            embed.add_field("📚 Read The Help Documents", f"with {help_command.mention()}")
            embed.add_field("💬 Join The Support Server", f"with {server_command.mention()}")
            embed.add_field("📨 Send Feedback For Inquiry", f"with {feedback_command.mention()}")
            embed.set_footer("Thanks for using Inquiry ❤️")
            await poll.latest_context.send(embed=embed, ephemeral=True)


def setup(bot):
    HelpExtension(bot)
