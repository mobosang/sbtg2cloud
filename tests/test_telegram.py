import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.errors import FloodWaitError

from app.store import Store
from app.telegram import CHECK_LIMIT, LISTENER_LOG, Telegram


def message(message_id, timestamp, share=None, edited=None, out=False):
    return SimpleNamespace(
        id=message_id, date=datetime.fromtimestamp(timestamp, timezone.utc),
        edit_date=datetime.fromtimestamp(edited, timezone.utc) if edited else None,
        out=out, raw_text='🎬 样本片名', entities=[],
        reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[
            SimpleNamespace(url='https://guangyapan.com/s/' + share)
        ])]) if share else None)


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.execute("INSERT INTO sources (id,title,enabled,enabled_at) VALUES (5,'group',1,940)")
        self.tg = Telegram(self.store, Path(self.tmp.name))
        self.tg.ready_at = 900.5
        self.tg.state = 'connected'
        self.tg.client = Mock()
        self.tg.client.is_connected.return_value = True
        self.clock = patch('app.telegram.time.time', return_value=1000)
        self.clock.start()

    async def asyncTearDown(self):
        self.clock.stop()
        self.store.db.close()
        self.tmp.cleanup()

    def test_listener_info_remains_enabled_when_playback_raises_parent_level(self):
        parent = logging.getLogger('uvicorn.error')
        self.addCleanup(parent.setLevel, parent.level)
        parent.setLevel(logging.WARNING)
        self.assertTrue(LISTENER_LOG.isEnabledFor(logging.INFO))

    async def test_live_edit_and_online_check_backfill_once_with_diagnostics(self):
        original = message(21, 950)
        edited = message(21, 950, 'late', edited=960)
        absent_update = message(22, 970, 'missing-event')
        self.tg.client.get_messages = AsyncMock(return_value=[
            original, edited, absent_update, message(23, 975, 'late')])
        with self.assertLogs(LISTENER_LOG, level='INFO') as audit:
            await self.tg.message(SimpleNamespace(chat_id=5, message=original))
            self.assertEqual(self.store.rows('SELECT id FROM jobs'), [])
            await self.tg.edited_message(SimpleNamespace(chat_id=5, message=edited))
            await self.tg.check_recent()
            await self.tg.check_recent()
        self.assertEqual(self.store.rows('SELECT share_id,message_id FROM jobs ORDER BY id'),
                         [{'share_id': 'late', 'message_id': 21}, {'share_id': 'missing-event', 'message_id': 22}])
        self.assertEqual(self.store.rows('SELECT message_id FROM duplicate_shares'), [{'message_id': 23}])
        self.assertTrue(any('id=21 via=live queued=0 duplicate=0' in row for row in audit.output))
        self.assertTrue(any('id=21 via=edit queued=1' in row for row in audit.output))
        self.assertTrue(any('id=22 via=check queued=1' in row for row in audit.output))
        self.assertEqual(len(audit.output), 4)
        self.assertNotIn('guangyapan.com', ' '.join(audit.output))
        self.tg.client.get_messages.assert_awaited_once_with(
            5, limit=CHECK_LIMIT, reverse=True, offset_date=datetime.fromtimestamp(939, timezone.utc))

    async def test_check_filters_before_reconnection_and_outgoing(self):
        self.tg.ready_at = 955.4
        self.tg.client.get_messages = AsyncMock(return_value=[
            message(10, 954, 'offline'), message(11, 957, 'out', out=True),
            message(12, 958, 'online')])
        await self.tg.check_recent()
        self.assertEqual(self.store.rows('SELECT share_id FROM jobs'), [{'share_id': 'online'}])
        self.assertEqual(self.tg.client.get_messages.await_args.kwargs['offset_date'].timestamp(), 954)

    async def test_in_flight_check_cannot_enqueue_after_reconnect_or_source_disable(self):
        async def after_reconnect(*args, **kwargs):
            self.tg.ready_at = 900.8
            return [message(30, 990, 'stale')]

        self.tg.client.get_messages = AsyncMock(side_effect=after_reconnect)
        await self.tg.check_recent()
        self.assertEqual(self.store.rows('SELECT id FROM jobs'), [])
        self.tg.last_checked.clear()

        async def after_disable(*args, **kwargs):
            self.store.execute('UPDATE sources SET enabled=0 WHERE id=5')
            return [message(31, 995, 'disabled')]

        self.tg.client.get_messages.side_effect = after_disable
        await self.tg.check_recent()
        self.assertEqual(self.store.rows('SELECT id FROM jobs'), [])

    async def test_check_respects_window_and_isolates_enqueue_failures(self):
        self.store.execute('UPDATE sources SET enabled_at=100 WHERE id=5')
        self.tg.ready_at = 200.5
        self.tg.client.get_messages = AsyncMock(return_value=[
            message(33, 699, 'outside-window'), message(34, 710, 'temporary'), message(35, 720, 'ok')])
        enqueue = self.store.enqueue

        def temporary_error(link, source, message_id, title):
            if message_id == 34:
                raise RuntimeError('SECRET')
            return enqueue(link, source, message_id, title)

        with patch.object(self.store, 'enqueue', side_effect=temporary_error), self.assertLogs(
                LISTENER_LOG, level='WARNING') as audit:
            await self.tg.check_recent()
        self.assertEqual(self.store.rows('SELECT share_id FROM jobs'), [{'share_id': 'ok'}])
        self.assertNotIn('SECRET', ' '.join(audit.output))
        self.assertEqual(self.tg.client.get_messages.await_args.kwargs['offset_date'].timestamp(), 699)

    async def test_check_rotates_sources_and_honors_account_flood_wait(self):
        self.store.execute("INSERT INTO sources (id,title,enabled,enabled_at) VALUES (6,'other',1,940)")
        self.tg.client.get_messages = AsyncMock(side_effect=[FloodWaitError(None, capture=90), []])
        await self.tg.check_recent()
        await self.tg.check_recent()
        self.assertEqual(self.tg.client.get_messages.await_count, 1)
        self.tg.check_resume_at = 0
        await self.tg.check_recent()
        await self.tg.check_recent()
        self.assertEqual([call.args[0] for call in self.tg.client.get_messages.await_args_list], [5, 6])
