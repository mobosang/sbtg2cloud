import asyncio
import time
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
                    self.store.update_job(jid, status='success', error='')
                else:
                    self.store.update_job(jid, next_at=time.time() + 5)
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
        self.store.update_job(jid, status='submitting', error='')
        try:
            task = await self.cloud.submit(access, ids, job['target_id'])
        except (CloudError, httpx.HTTPError):
            # A timeout/5xx can happen AFTER the cloud accepted the request.
            self.store.update_job(jid, status='uncertain', error='提交未取得明确回执，可能已转存。请核对云盘后手动重试，避免重复文件')
            return
        self.store.update_job(jid, status='submitted', remote_task=task, next_at=time.time() + 2)

    async def run(self):
        while True:
            if self.store.get('cloud_token'):
                jobs = self.store.rows("SELECT * FROM jobs WHERE status IN ('queued','submitted') AND next_at<=? ORDER BY next_at,id LIMIT 1", (time.time(),))
                if jobs:
                    try:
                        await self.process(jobs[0])
                    except Exception:
                        self.store.update_job(jobs[0]['id'], status='uncertain', error='处理异常，请检查任务与云盘状态')
            await asyncio.sleep(1)
