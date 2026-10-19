import asyncio
import time
import uuid
import httpx
from .cloud import CloudError, TaskFailed


class Worker:
    def __init__(self, store, cloud):
        self.store, self.cloud = store, cloud

    async def process(self, job):
        jid = job['id']
        if job['remote_task']:
            try:
                if await self.cloud.task(job['remote_task']):
                    self.store.update_job(jid, status='success', error='', completed_at=time.time())
                    if job['cleanup_enabled'] and job['cleanup_folder']:
                        try:
                            manifest = await self.cloud.cleanup_manifest(job['target_id'], job['cleanup_folder'], job['cleanup_name'])
                            self.store.update_job(jid, cleanup_manifest=manifest, cleanup_state='pending')
                        except Exception:
                            self.store.update_job(jid, cleanup_state='blocked', cleanup_error='转存已完成，但未能记录目录内容；不会自动删除')
                else:
                    self.store.update_job(jid, next_at=time.time() + 30)
            except (CloudError, httpx.HTTPError) as error:
                if isinstance(error, TaskFailed):
                    self.store.update_job(jid, status='failed', error=str(error))
                else:
                    self.store.update_job(jid, next_at=time.time() + 60, error='查询进度暂不可用；保留原云端任务，稍后继续查询')
            return
        attempts = job['attempts'] + 1
        self.store.update_job(jid, status='preparing', attempts=attempts)
        try:
            access, ids, title = await self.cloud.prepare(job['share_id'], job['code'])
            if not job['resource_name'] and title:
                self.store.update_job(jid, resource_name=title)
        except (CloudError, httpx.HTTPError) as error:
            retryable = isinstance(error, httpx.HTTPError) or error.retryable
            self.store.update_job(jid, status='queued' if retryable and attempts < 3 else 'failed',
                                  next_at=time.time() + 15 * 2 ** attempts,
                                  error=str(error) if isinstance(error, CloudError) else '网络连接失败')
            return
        parent = job['target_id']
        if job['cleanup_enabled']:
            try:
                if job['cleanup_folder']:
                    manifest = await self.cloud.cleanup_manifest(parent, job['cleanup_folder'], job['cleanup_name'])
                    if manifest != '[]':
                        raise CloudError('临时目录已有内容，请手动核对后移走内容再重试')
                    parent = job['cleanup_folder']
                else:
                    name = 'sbtg-' + str(jid) + '-' + uuid.uuid4().hex
                    # Persist before a mutating request, including mkdir.
                    self.store.update_job(jid, status='submitting', cleanup_name=name)
                    parent = await self.cloud.create_cleanup_folder(parent, name)
                    self.store.update_job(jid, cleanup_folder=parent)
                    if await self.cloud.cleanup_manifest(job['target_id'], parent, name) != '[]':
                        raise CloudError('新建临时目录非空，停止转存')
            except Exception:
                self.store.update_job(jid, status='uncertain', error='临时目录创建或检查未完成，请先核对云盘中的 sbtg 目录再重试')
                return
        self.store.update_job(jid, status='submitting', error='')
        try:
            task = await self.cloud.submit(access, ids, parent)
        except (CloudError, httpx.HTTPError):
            # A timeout/5xx can happen AFTER the cloud accepted the request.
            self.store.update_job(jid, status='uncertain', error='提交未取得明确回执，可能已转存。请核对云盘后手动重试，避免重复文件')
            return
        self.store.update_job(jid, status='submitted', remote_task=task, next_at=time.time() + 15)

    async def cleanup(self, job):
        jid = job['id']
        if job['cleanup_state'] == 'submitted':
            try:
                if await self.cloud.task(job['cleanup_task']):
                    self.store.update_job(jid, cleanup_state='done', cleanup_error='')
                else:
                    self.store.update_job(jid, cleanup_next=time.time() + 60)
            except TaskFailed:
                self.store.update_job(jid, cleanup_state='blocked', cleanup_error='云盘删除任务失败，请手动核对；不会重新提交')
            except Exception:
                self.store.update_job(jid, cleanup_next=time.time() + 60, cleanup_error='删除进度暂不可查，只查询原任务，不会重新提交')
            return
        if (not self.store.get('auto_cleanup', False) or job['status'] != 'success'
            or job['cleanup_state'] != 'pending' or not job['cleanup_enabled']
            or not job['completed_at'] or time.time() - job['completed_at'] < 3600):
            return
        try:
            manifest = await self.cloud.cleanup_manifest(job['target_id'], job['cleanup_folder'], job['cleanup_name'])
            if not job['cleanup_manifest'] or manifest != job['cleanup_manifest']:
                raise CloudError('目录内容已变化，停止自动删除')
        except Exception:
            self.store.update_job(jid, cleanup_state='blocked', cleanup_error='目录位置或内容变化，或核对失败；请手动检查，不会自动删除')
            return
        # A user can turn cleanup off while the read-only check is awaiting the network.
        if not self.store.get('auto_cleanup', False):
            return
        self.store.update_job(jid, cleanup_state='deleting', cleanup_error='')
        try:
            task = await self.cloud.delete_cleanup_folder(job['cleanup_folder'])
        except Exception:
            self.store.update_job(jid, cleanup_state='uncertain', cleanup_error='删除结果不明，请手动核对；不会再次删除')
            return
        self.store.update_job(jid, cleanup_state='submitted', cleanup_task=task, cleanup_next=time.time() + 30)

    async def cleanup_due(self):
        # Wall-clock timestamps are persisted; no in-memory one-hour timers.
        jobs = self.store.rows("""SELECT * FROM jobs WHERE status='success' AND cleanup_enabled=1
            AND cleanup_next<=? AND (cleanup_state='submitted' OR
            (cleanup_state='pending' AND completed_at<=? AND ?=1)) ORDER BY completed_at,id LIMIT 1""",
            (time.time(), time.time() - 3600, bool(self.store.get('auto_cleanup', False))))
        if jobs:
            await self.cleanup(jobs[0])

    async def run(self):
        next_cleanup_check = 0.0
        while True:
            if self.store.get('cloud_token'):
                jobs = self.store.rows("SELECT * FROM jobs WHERE status IN ('queued','submitted') AND next_at<=? ORDER BY next_at,id LIMIT 1", (time.time(),))
                if jobs:
                    try:
                        await self.process(jobs[0])
                    except Exception:
                        self.store.update_job(jobs[0]['id'], status='uncertain', error='处理异常，请检查任务与云盘状态')
                if time.monotonic() >= next_cleanup_check:
                    await self.cleanup_due()
                    next_cleanup_check = time.monotonic() + 30
            await asyncio.sleep(1)
