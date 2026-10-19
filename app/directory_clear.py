"""User-confirmed clearing of a destination's children, never the parent itself."""
import asyncio
import json
import time
import uuid
from .cloud import TaskFailed
from .cleanup import file_spec


class DirectoryClear:
    def __init__(self, store, cloud):
        self.store, self.cloud = store, cloud
        self.reconcile_completed()

    def reconcile_completed(self):
        """Only a confirmed successful clear receipt can settle overlapping cleanup."""
        completed = {}
        for part in self.store.rows("SELECT c.parent_id,p.ids FROM directory_clear_parts p JOIN directory_clears c ON c.id=p.clear_id WHERE p.state='done'"):
            completed.setdefault(part['parent_id'], set()).update(json.loads(part['ids']))
        for job in self.store.rows("SELECT * FROM jobs WHERE status='success' AND cleanup_state IN ('pending','blocked') AND cleanup_task=''"):
            try:
                ids = {x['id'] for x in json.loads(job['cleanup_targets'])} if job['cleanup_targets'] else {job['cleanup_folder']}
            except (ValueError, KeyError, TypeError):
                continue
            if ids and '' not in ids and ids <= completed.get(job['target_id'], set()):
                self.store.update_job(job['id'], cleanup_state='done', cleanup_error='已通过一键清空移入回收站')

    def targets(self):
        default = self.store.get('target', {'id': '', 'name': '/'})
        targets = {default['id']: default['name']}
        for row in self.store.rows('SELECT target_id,target_name FROM sources WHERE target_id IS NOT NULL UNION SELECT target_id,target_name FROM jobs'):
            targets.setdefault(row['target_id'], row['target_name'])
        return [{'id': key, 'name': value} for key, value in targets.items() if key]

    def busy(self):
        return bool(self.cloud.operation_lock.locked() or self.store.rows("SELECT id FROM jobs WHERE status IN ('preparing','submitting','submitted') OR cleanup_state IN ('deleting','submitted') LIMIT 1")
                    or self.store.rows("SELECT id FROM directory_clears WHERE state IN ('queued','running') LIMIT 1"))

    async def preview(self, parent):
        target = next((x for x in self.targets() if x['id'] == parent), None)
        if not target or not parent:
            raise ValueError('请选择已配置的转存目录；不支持清空云盘根目录')
        if self.busy():
            raise ValueError('有云端任务正在处理，请完成后再清空目录')
        try:
            rows = await asyncio.wait_for(
                self.cloud.pages('/file/get_file_list', {'parentId': parent}), timeout=45)
        except TimeoutError:
            raise ValueError('读取目录超时，可能正在等待云盘限流或网络响应；未提交删除，请稍后重新预览') from None
        items = [file_spec(x) for x in rows]
        if any(not x['id'] or x['id'] == parent or x['kind'] not in (1, 2) for x in items):
            raise ValueError('目录内容无法完整识别，已停止')
        claimed = {x['file_id'] for x in self.store.rows('SELECT file_id FROM directory_clear_claims')}
        if any(x['id'] in claimed for x in items):
            raise ValueError('目录中有已经提交过清空的目标，请先核对原任务和回收站；不会重复删除')
        if self.busy():
            raise ValueError('读取期间有任务开始处理，请稍后重新预览')
        token = uuid.uuid4().hex
        self.store.execute('INSERT INTO directory_clears(id,parent_id,parent_name,items,state,created) VALUES (?,?,?,?,?,?)',
                           (token,parent,target['name'],json.dumps(items,ensure_ascii=False),'draft',time.time()))
        return {'id':token,'name':target['name'],'items':items,'count':len(items),
                'folders':sum(x['kind']==2 for x in items),'files':sum(x['kind']==1 for x in items)}

    def confirm(self, token):
        rows = self.store.rows('SELECT * FROM directory_clears WHERE id=?',(token,))
        if not rows:
            raise ValueError('预览不存在，请重新预览')
        job = rows[0]
        if job['state'] != 'draft':
            return  # Duplicate clicks must never create new deletion requests.
        if time.time()-job['created'] > 300:
            raise ValueError('预览已超过五分钟，请重新核对目录')
        if self.busy():
            raise ValueError('有云端任务正在处理，请稍后重新预览')
        ids = [x['id'] for x in json.loads(job['items'])]
        with self.store.db:
            for index in range(0,len(ids),100):
                self.store.db.execute('INSERT INTO directory_clear_parts(clear_id,part,ids,state) VALUES (?,?,?,?)',
                                      (token,index//100,json.dumps(ids[index:index+100]),'queued'))
            self.store.db.execute('UPDATE directory_clears SET state=? WHERE id=?',('queued' if ids else 'done',token))

    def latest(self):
        rows = self.store.rows("SELECT id,parent_name,state,error,created FROM directory_clears WHERE state!='draft' ORDER BY created DESC LIMIT 1")
        if not rows:
            return None
        job = rows[0]
        parts = self.store.rows('SELECT state,COUNT(*) n FROM directory_clear_parts WHERE clear_id=? GROUP BY state',(job['id'],))
        job['done'] = sum(x['n'] for x in parts if x['state']=='done')
        job['total'] = sum(x['n'] for x in parts)
        return job

    def stop(self, token, state, error):
        self.store.execute('UPDATE directory_clears SET state=?,error=? WHERE id=?',(state,error,token))

    async def step(self):
        jobs = self.store.rows("SELECT * FROM directory_clears WHERE state IN ('queued','running') ORDER BY created LIMIT 1")
        if not jobs:
            return False
        job = jobs[0]
        token = job['id']
        parts = self.store.rows("SELECT * FROM directory_clear_parts WHERE clear_id=? AND state!='done' ORDER BY part LIMIT 1",(token,))
        if not parts:
            self.stop(token,'done','')
            self.cloud.history_cache.clear()
            return True
        part = parts[0]
        if part['state'] == 'submitted':
            if time.time() < part['next_at']:
                return True
            try:
                if await self.cloud.task(part['task']):
                    self.store.execute("UPDATE directory_clear_parts SET state='done' WHERE clear_id=? AND part=?",(token,part['part']))
                    self.reconcile_completed()
                else:
                    self.store.execute('UPDATE directory_clear_parts SET next_at=? WHERE clear_id=? AND part=?',(time.time()+60,token,part['part']))
            except TaskFailed:
                self.stop(token,'blocked','云端删除失败，可能部分完成，请核对；不会重发')
            except Exception:
                self.store.execute('UPDATE directory_clear_parts SET next_at=? WHERE clear_id=? AND part=?',(time.time()+60,token,part['part']))
            return True
        if part['state'] != 'queued':
            self.stop(token,'uncertain','提交状态不明，请核对；不会重发')
            return True
        ids = json.loads(part['ids'])
        claimed = {x['file_id'] for x in self.store.rows('SELECT file_id FROM directory_clear_claims')}
        if set(ids) & claimed:
            self.stop(token,'blocked','部分目标已提交过删除，停止重复删除，请核对原任务')
            return True
        try:
            rows = await self.cloud.pages('/file/get_file_list',{'parentId':job['parent_id']})
            live = {x['id']:x for x in map(file_spec,rows)}
            expected = {x['id']:x for x in json.loads(job['items'])}
            if any(fid not in live or live[fid] != expected[fid] for fid in ids):
                raise ValueError('changed')
        except Exception:
            self.stop(token,'blocked','目录已变化或无法核对，已停止后续删除；请重新预览')
            return True
        # Persist before the destructive call. Suspend overlapping automatic cleanup.
        with self.store.db:
            self.store.db.executemany('INSERT INTO directory_clear_claims VALUES (?,?)',[(fid,token) for fid in ids])
            self.store.db.executemany('INSERT OR REPLACE INTO cleanup_claims VALUES (?,-1)',[(fid,) for fid in ids])
            self.store.db.execute("UPDATE directory_clears SET state='running' WHERE id=?",(token,))
            self.store.db.execute("UPDATE directory_clear_parts SET state='deleting' WHERE clear_id=? AND part=?",(token,part['part']))
            self.store.db.execute("UPDATE jobs SET cleanup_state='blocked',cleanup_error='该目录已执行一键清空，请核对资源' WHERE target_id=? AND cleanup_state='pending'",(job['parent_id'],))
        try:
            task = await self.cloud.delete_cleanup_targets(ids)
        except Exception:
            self.stop(token,'uncertain','删除结果不明，请核对回收站；不会重发')
            return True
        self.store.execute("UPDATE directory_clear_parts SET state='submitted',task=?,next_at=? WHERE clear_id=? AND part=?",(task,time.time()+60,token,part['part']))
        return True
