from typing import TypedDict

import attr
from naff import Snowflake_Type
from naff.client.mixins.serialization import DictSerializationMixin


class GuildDataPayload(TypedDict):
    id: Snowflake_Type
    thank_you_sent: bool
    blacklisted: bool
    blacklisted_users: list[Snowflake_Type]
    suggestion_channel: Snowflake_Type
    suggestion_ping_role: Snowflake_Type
    suggestion_create_thread: bool


@attr.s(auto_attribs=True, on_setattr=[attr.setters.convert, attr.setters.validate])
class GuildData(DictSerializationMixin):
    id: Snowflake_Type = attr.field()
    thank_you_sent: bool = attr.field(default=True)
    blacklisted: bool = attr.field(default=False)
    blacklisted_users: list[Snowflake_Type] = attr.field(factory=list)
    suggestion_channel: Snowflake_Type = attr.field(default=None)
    suggestion_ping_role: Snowflake_Type = attr.field(default=None)
    suggestion_create_thread: bool = attr.field(default=False)
