from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from kleinanzeigen_bot.runtime.controls import handle_update
from kleinanzeigen_bot.runtime.preferences import PreferenceStore
from kleinanzeigen_bot.runtime.queue import RunQueue


class RecordingBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **payload):
        self.messages.append(payload)
        return SimpleNamespace(message_id=len(self.messages))


class Events:
    def emit(self, *args):
        pass


def update(text, *, actor=42, chat=-1, ident=1):
    return SimpleNamespace(
        update_id=ident,
        callback_query=None,
        message=SimpleNamespace(
            text=text, chat_id=chat, message_id=ident, from_user=SimpleNamespace(id=actor)
        ),
    )


@pytest.fixture
def controls(tmp_path):
    queue = RunQueue(tmp_path / "queue.sqlite3")
    store = PreferenceStore(queue)
    bot = RecordingBot()

    async def send(text, **kwargs):
        await handle_update(
            update(text, **kwargs),
            queue,
            bot,
            "-1",
            [42],
            Events(),
            preferences=store,
            template="Guten Tag,\nI am interested in {title}.",
        )

    yield queue, store, bot, send
    queue.close()


@pytest.mark.asyncio
async def test_search_commands_change_persisted_configuration(controls):
    queue, store, bot, send = controls
    await send("/city Hannover,Garbsen", ident=1)
    await send("/search@ExampleBot Balkon", ident=2)
    await send("/budget 600", ident=3)
    await send("/area 30", ident=4)
    await send("/settings", ident=5)
    value = store.load()
    assert value.cities == ("hannover", "garbsen")
    assert value.query == "Balkon"
    assert value.max_rent == 600
    assert value.min_area == 30
    assert not value.auto_reply
    assert "Balkon" in bot.messages[-1]["text"]
    assert "600.00" in bot.messages[-1]["text"]
    assert queue.status()["jobs"] == {}


@pytest.mark.asyncio
async def test_unauthorized_actors_and_other_chats_cannot_change_settings(controls):
    _, store, bot, send = controls
    await send("/budget 999", actor=7)
    await send("/replies on", chat=-2)
    assert store.load().max_rent == 520
    assert not store.load().auto_reply
    assert bot.messages == []


@pytest.mark.asyncio
async def test_invalid_command_preserves_previous_value_and_reports_problem(controls):
    _, store, bot, send = controls
    await send("/budget 600", ident=1)
    await send("/budget NaN", ident=2)
    assert store.load().max_rent == 600
    assert "Invalid" in bot.messages[-1]["text"]
    await send("/replies potato", ident=3)
    assert not store.load().auto_reply


@pytest.mark.asyncio
async def test_repeated_commands_do_not_undo_newer_settings(controls):
    _, store, _, send = controls
    await send("/budget 600", ident=1)
    await send("/budget 700", ident=2)
    await send("/budget 600", ident=1)
    assert store.load().max_rent == 700


@pytest.mark.asyncio
async def test_template_and_replies_are_explicit_and_do_not_enqueue_run(controls):
    queue, store, bot, send = controls
    await send("/template", ident=1)
    assert "Guten Tag," in bot.messages[-1]["text"]
    await send("/replies on", ident=2)
    assert store.load().auto_reply
    await send("/search -", ident=3)
    assert store.load().query == ""
    assert queue.status()["jobs"] == {}


@pytest.mark.asyncio
async def test_expired_callback_acknowledgement_does_not_block_following_pause(controls):
    queue, store, bot, send = controls

    async def expired(_):
        raise BadRequest("Query is too old and response timeout expired or query ID is invalid")

    query = SimpleNamespace(message=SimpleNamespace(chat_id=-1), from_user=SimpleNamespace(id=42),
                            data="r:run", answer=expired)
    await handle_update(SimpleNamespace(callback_query=query, update_id=10), queue, bot,
                        "-1", [42], Events(), preferences=store)
    await send("/pause", ident=11)
    assert queue.get("paused")
    assert queue.status()["jobs"]["queued"] == 1
