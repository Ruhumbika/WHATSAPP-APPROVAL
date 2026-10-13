from datetime import datetime, timezone
from unittest.mock import Mock, patch
from dataclasses import replace
import unittest
import test_chat_menu as fixture
from approval_gateway import admin_controls as controls
from approval_gateway.integrations.permissions import WorkflowDenied
from approval_gateway.workflow_notifications import send_job


class QuietHoursTests(unittest.TestCase):
    setUp = fixture.ChatMenuTests.setUp
    response = fixture.ChatMenuTests.response
    jobs = fixture.ChatMenuTests.jobs
    send = fixture.ChatMenuTests.send
    click = fixture.ChatMenuTests.click

    def change(self, key, value):
        revision, _ = controls.read(self.settings)
        controls.apply(self.settings, self.phone, f'change-{revision}', key, value, revision)

    def test_boundaries_overnight_and_daytime(self):
        self.change('quiet_enabled', True)
        self.assertEqual(controls.quiet_until(self.settings, datetime(2026,10,13,15,tzinfo=timezone.utc)), '2026-10-14T05:00:00+00:00')
        self.assertIsNone(controls.quiet_until(self.settings, datetime(2026,10,14,5,tzinfo=timezone.utc)))
        self.change('quiet_start', '12:00')
        self.change('quiet_end', '14:00')
        self.assertEqual(controls.quiet_until(self.settings, datetime(2026,10,13,9,tzinfo=timezone.utc)), '2026-10-13T11:00:00+00:00')
        self.assertIsNone(controls.quiet_until(self.settings, datetime(2026,10,13,11,tzinfo=timezone.utc)))

    def test_admin_buttons_custom_time_and_confirmation(self):
        self.send('ADMIN')
        self.click('Reminder settings')
        self.click('Quiet hours')
        self.click('Set start time')
        self.send('20:30')
        self.assertEqual(controls.read(self.settings)[1]['quiet_start'], '18:00')
        self.click('Confirm change')
        self.assertEqual(controls.read(self.settings)[1]['quiet_start'], '20:30')

    def test_invalid_time_equal_time_and_unauthorized_admin(self):
        for value in ('25:00','8:00','08:60','hello'):
            with self.assertRaises(WorkflowDenied): controls.validate('quiet_start', value)
        with self.assertRaises(WorkflowDenied): self.change('quiet_start', '08:00')
        with self.assertRaises(WorkflowDenied):
            controls.apply(self.settings, '255700009999', 'bad', 'quiet_enabled', True, 0)
        self.assertFalse(controls.read(self.settings)[1]['quiet_enabled'])

    def test_quiet_does_not_send_or_consume_attempt(self):
        client = Mock()
        with patch('approval_gateway.admin_controls.quiet_until', return_value='2026-10-14T05:00:00+00:00'), patch('approval_gateway.workflow_notifications.prepare_send') as prepare:
            send_job(self.settings, client, {'id':999, 'kind':'reminder'})
        prepare.assert_not_called()
        client.send_workflow_template.assert_not_called()
