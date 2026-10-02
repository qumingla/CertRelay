import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.db import Database, dumps, DEFAULT_SETTINGS
from app.events import EventHub
from app.jobs import create_job, finish_job, recover_stale_jobs
from app.live_ops import DomainBundle
from app.scheduler import reconcile, queue_deployments, run_scheduler
from app.timeutil import iso_now, to_iso, utc_now


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'test.db')
        self.db.init()
        self.hub = EventHub(self.db)
        self.app = SimpleNamespace(state=SimpleNamespace(db=self.db, event_hub=self.hub, config=None))
        now = iso_now()
        self.db.execute("INSERT INTO dns_channels VALUES ('c', 'DNS', 'dns_cf', '{}', ?, ?)", (now, now))
        self.db.execute("INSERT INTO domains (id, domain, dns_channel_id, created_at, updated_at) VALUES ('d', 'example.com', 'c', ?, ?)", (now, now))
        self.db.execute("INSERT INTO nodes (id, name, token_hash, created_at, updated_at) VALUES ('n', 'Node', 'hash', ?, ?)", (now, now))
        self.db.execute("INSERT INTO node_assignments (id, node_id, domain_id, created_at, updated_at) VALUES ('a', 'n', 'd', ?, ?)", (now, now))
        self.bundle = DomainBundle(Path('/unused'), Path('/unused'), 'new-sha', to_iso(utc_now() + timedelta(days=90)), Path(self.tmp.name) / 'bundle')

    async def test_issue_and_queue_offline_node_once(self):
        with patch('app.scheduler.run_domain_script', new_callable=AsyncMock, return_value=self.bundle) as run:
            await reconcile(self.app)
            await reconcile(self.app)
        run.assert_awaited_once()
        self.assertTrue(run.call_args.kwargs['force_reissue'])
        self.assertEqual(len(self.db.query_all('SELECT * FROM node_commands')), 1)
        self.assertEqual(self.db.query_one("SELECT desired_sha256 FROM node_assignments")['desired_sha256'], 'new-sha')

    async def test_disabled_and_not_due_are_skipped(self):
        self.db.execute("UPDATE domains SET enabled = 0")
        with patch('app.scheduler.run_domain_script', new_callable=AsyncMock) as run:
            await reconcile(self.app)
            self.db.execute("UPDATE domains SET enabled = 1, expires_at = ?, cert_sha256 = 'old'", (self.bundle.expires_at,))
            await reconcile(self.app)
        run.assert_not_awaited()

    async def test_configured_threshold_is_used(self):
        settings = {**DEFAULT_SETTINGS, 'acme': {**DEFAULT_SETTINGS['acme'], 'defaultRenewDays': 30}}
        self.db.execute("UPDATE app_settings SET value = ? WHERE key = 'settings'", (dumps(settings),))
        self.db.execute("UPDATE domains SET expires_at = ?, cert_sha256 = 'old'", (to_iso(utc_now() + timedelta(days=20)),))
        with patch('app.scheduler.run_domain_script', new_callable=AsyncMock, return_value=self.bundle) as run:
            await reconcile(self.app)
        run.assert_awaited_once()

    async def test_renew_failure_cooldown(self):
        with patch('app.scheduler.run_domain_script', new_callable=AsyncMock, side_effect=RuntimeError('DNS failure')) as run:
            await reconcile(self.app)
            await reconcile(self.app)
        run.assert_awaited_once()
        self.assertEqual(self.db.query_one('SELECT status FROM jobs')['status'], 'failed')
        self.assertEqual(self.db.query_all('SELECT * FROM node_commands'), [])

    async def test_manual_operation_blocks_automatic_renewal(self):
        create_job(self.db, self.hub, 'issue', 'bulk')
        with patch('app.scheduler.run_domain_script', new_callable=AsyncMock) as run:
            await reconcile(self.app)
        run.assert_not_awaited()

    async def test_deployed_and_disabled_assignments_are_skipped(self):
        self.db.execute("UPDATE node_assignments SET desired_sha256 = 'same', deployed_sha256 = 'same', status = 'synced'")
        queue_deployments(self.app)
        self.db.execute("UPDATE domains SET enabled = 0")
        self.db.execute("UPDATE node_assignments SET desired_sha256 = 'new', status = 'pending'")
        queue_deployments(self.app)
        self.assertEqual(self.db.query_all('SELECT * FROM node_commands'), [])

    async def test_failed_deployment_retries_after_cooldown(self):
        self.db.execute("UPDATE node_assignments SET desired_sha256 = 'new'")
        queue_deployments(self.app)
        self.db.execute("UPDATE node_commands SET status = 'failed'")
        queue_deployments(self.app)
        self.assertEqual(len(self.db.query_all('SELECT * FROM node_commands')), 1)
        self.db.execute("UPDATE node_commands SET updated_at = ?", (to_iso(utc_now() - timedelta(minutes=6)),))
        queue_deployments(self.app)
        self.assertEqual(len(self.db.query_all('SELECT * FROM node_commands')), 2)

    async def test_job_duration_uses_utc(self):
        job = create_job(self.db, self.hub, 'renew', 'd')
        self.db.execute("UPDATE jobs SET started_at = ? WHERE id = ?", (to_iso(utc_now() - timedelta(seconds=5)), job['id']))
        result = finish_job(self.db, self.hub, job['id'])
        self.assertGreaterEqual(result['durationMs'], 5000)
        self.assertLess(result['durationMs'], 10000)

    async def test_saving_assignments_preserves_deployment(self):
        from app.routers.admin import update_node_assignments
        from app.schemas import AssignmentUpdate
        self.db.execute("UPDATE node_assignments SET deployed_sha256 = 'deployed', status = 'synced'")
        with patch('app.routers.admin._node_detail', return_value={}):
            await update_node_assignments('n', AssignmentUpdate(domainIds=['d', 'd']), SimpleNamespace(app=self.app), self.db, self.hub)
        row = self.db.query_one('SELECT * FROM node_assignments')
        self.assertEqual(row['id'], 'a')
        self.assertEqual(row['deployed_sha256'], 'deployed')

    async def test_invalid_assignment_update_is_atomic(self):
        from fastapi import HTTPException
        from app.routers.admin import update_node_assignments
        from app.schemas import AssignmentUpdate
        with self.assertRaises(HTTPException):
            await update_node_assignments('n', AssignmentUpdate(domainIds=['missing']), SimpleNamespace(app=self.app), self.db, self.hub)
        self.assertEqual(self.db.query_one('SELECT id FROM node_assignments')['id'], 'a')

    async def test_old_node_report_does_not_mark_new_certificate_synced(self):
        from app.routers.node import report
        from app.schemas import NodeReport, NodeReportItem
        self.db.execute("UPDATE node_assignments SET desired_sha256 = 'new'")
        await report(NodeReport(items=[NodeReportItem(domainId='d', status='synced', deployedSha256='old')]),
                     {'id': 'n', 'name': 'Node'}, self.db, self.hub)
        self.assertEqual(self.db.query_one('SELECT status FROM node_assignments')['status'], 'pending')

    async def test_interrupted_job_does_not_block_renewal_forever(self):
        job = create_job(self.db, self.hub, 'renew', 'd')
        old = to_iso(utc_now() - timedelta(hours=2))
        self.db.execute('UPDATE jobs SET started_at = ?, updated_at = ? WHERE id = ?', (old, old, job['id']))
        with patch('app.scheduler.run_domain_script', new_callable=AsyncMock, return_value=self.bundle) as run:
            await reconcile(self.app)
        run.assert_awaited_once()
        self.assertEqual(self.db.query_one('SELECT status FROM jobs WHERE id = ?', (job['id'],))['status'], 'failed')

    async def test_explicit_deletion_is_not_immediately_undone(self):
        from app.routers.admin import _queue_node_command
        self.db.execute("UPDATE node_assignments SET desired_sha256 = 'new'")
        _queue_node_command(self.db, self.hub, 'n', 'delete_domains', ['d'])
        self.db.execute("UPDATE node_commands SET status = 'completed', completed_at = ?", (iso_now(),))
        queue_deployments(self.app)
        self.assertEqual(len(self.db.query_all('SELECT * FROM node_commands')), 1)

    def queue_command(self, command_type='sync_domains'):
        from app.routers.admin import _queue_node_command
        job = _queue_node_command(self.db, self.hub, 'n', command_type, ['d'])
        command = self.db.query_one('SELECT * FROM node_commands WHERE job_id = ?', (job['id'],))
        return job, command

    def age_command(self, command):
        self.db.execute('UPDATE node_commands SET created_at = ? WHERE id = ?',
                        (to_iso(utc_now() - timedelta(minutes=31)), command['id']))

    async def test_expired_node_command_is_failed_and_not_delivered(self):
        from app.routers.node import commands
        job, command = self.queue_command()
        self.age_command(command)
        result = await commands({'id': 'n'}, self.db, self.hub)
        self.assertEqual(result['commands'], [])
        self.assertEqual(self.db.query_one('SELECT status FROM node_commands')['status'], 'failed')
        row = self.db.query_one('SELECT * FROM jobs WHERE id = ?', (job['id'],))
        self.assertEqual(row['status'], 'failed')
        self.assertIsNotNone(row['ended_at'])
        self.assertIn('30 分钟', row['log_text'])
        self.assertIn('超时', row['error'])

    async def test_late_ack_does_not_overwrite_timeout(self):
        from app.routers.node import ack_command
        from app.schemas import NodeCommandAck
        job, command = self.queue_command()
        self.age_command(command)
        await ack_command(command['id'], NodeCommandAck(status='completed'),
                          {'id': 'n', 'name': 'Node'}, self.db, self.hub)
        self.assertEqual(self.db.query_one('SELECT status FROM node_commands')['status'], 'failed')
        self.assertEqual(self.db.query_one('SELECT status FROM jobs WHERE id = ?', (job['id'],))['status'], 'failed')

    async def test_recent_offline_command_keeps_waiting(self):
        job, command = self.queue_command()
        recover_stale_jobs(self.db, self.hub)
        self.assertEqual(self.db.query_one('SELECT status FROM node_commands')['status'], 'pending')
        self.assertEqual(self.db.query_one('SELECT status FROM jobs WHERE id = ?', (job['id'],))['status'], 'running')

    async def test_on_time_ack_still_completes_normally(self):
        from app.routers.node import ack_command
        from app.schemas import NodeCommandAck
        job, command = self.queue_command()
        with patch('app.routers.node._notify_node_command_result', new_callable=AsyncMock):
            await ack_command(command['id'], NodeCommandAck(status='completed', summary='done'),
                              {'id': 'n', 'name': 'Node'}, self.db, self.hub)
        self.assertEqual(self.db.query_one('SELECT status FROM node_commands')['status'], 'completed')
        self.assertEqual(self.db.query_one('SELECT status FROM jobs WHERE id = ?', (job['id'],))['status'], 'success')

    async def test_missing_command_closes_old_deployment(self):
        job = create_job(self.db, self.hub, 'deploy', 'n')
        self.db.execute('UPDATE jobs SET started_at = ? WHERE id = ?',
                        (to_iso(utc_now() - timedelta(days=60)), job['id']))
        recover_stale_jobs(self.db, self.hub)
        self.assertEqual(self.db.query_one('SELECT status FROM jobs')['status'], 'failed')
        self.assertIn('已丢失', self.db.query_one('SELECT error FROM jobs')['error'])

    async def test_terminal_command_repairs_unfinished_job(self):
        for command_status, expected in [('completed', 'success'), ('failed', 'failed')]:
            with self.subTest(command_status=command_status):
                job, command = self.queue_command()
                self.db.execute('UPDATE node_commands SET status = ?, last_error = ? WHERE id = ?',
                                (command_status, 'node failure' if expected == 'failed' else None, command['id']))
                recover_stale_jobs(self.db, self.hub)
                self.assertEqual(self.db.query_one('SELECT status FROM jobs WHERE id = ?', (job['id'],))['status'], expected)

    async def test_delete_and_upgrade_commands_also_expire(self):
        for job_type, command_type in [('delete', 'delete_domains'), ('upgrade', 'upgrade_agent')]:
            with self.subTest(job_type=job_type):
                job, command = self.queue_command('delete_domains')
                self.db.execute('UPDATE jobs SET type = ? WHERE id = ?', (job_type, job['id']))
                self.db.execute('UPDATE node_commands SET type = ? WHERE id = ?', (command_type, command['id']))
                self.age_command(command)
                recover_stale_jobs(self.db, self.hub)
                self.assertEqual(self.db.query_one('SELECT status FROM jobs WHERE id = ?', (job['id'],))['status'], 'failed')

    async def test_recent_log_does_not_extend_node_deadline(self):
        job, command = self.queue_command()
        self.age_command(command)
        self.db.execute('UPDATE jobs SET updated_at = ? WHERE id = ?', (iso_now(), job['id']))
        recover_stale_jobs(self.db, self.hub)
        self.assertEqual(self.db.query_one('SELECT status FROM jobs WHERE id = ?', (job['id'],))['status'], 'failed')

    async def test_finishing_is_idempotent(self):
        job = create_job(self.db, self.hub, 'deploy', 'n')
        finish_job(self.db, self.hub, job['id'], 'failed', 'timeout')
        before = self.db.query_one('SELECT * FROM jobs WHERE id = ?', (job['id'],))
        finish_job(self.db, self.hub, job['id'], 'success')
        self.assertEqual(self.db.query_one('SELECT * FROM jobs WHERE id = ?', (job['id'],)), before)

    async def test_watchdog_runs_while_renewal_is_busy(self):
        self.app.state.config = SimpleNamespace(db_path=self.db.path)
        job, command = self.queue_command()
        renewal_started = asyncio.Event()
        real_sleep = asyncio.sleep
        ticks = 0

        async def busy_renewal(app):
            renewal_started.set()
            await asyncio.Event().wait()

        async def advance_tick(seconds):
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                await real_sleep(0)
                self.assertTrue(renewal_started.is_set())
                self.age_command(command)
            else:
                raise asyncio.CancelledError

        with patch('app.scheduler.reconcile', side_effect=busy_renewal), patch('app.scheduler.asyncio.sleep', side_effect=advance_tick):
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler(self.app)
        self.assertEqual(self.db.query_one('SELECT status FROM jobs WHERE id = ?', (job['id'],))['status'], 'failed')
