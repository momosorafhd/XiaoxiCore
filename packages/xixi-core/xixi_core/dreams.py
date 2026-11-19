"""Per-calendar-day review coverage; a global queue cursor cannot cover several days."""
import hashlib
import uuid
from .store import encode, decode


class DreamsMixin:
    def stage_summaries(self):
        """Current effective summaries are Core state, never Library categories."""
        return self.store.get('stage-summaries', {
            'version': 0, 'selfUnderstanding': '', 'ownerUnderstanding': '',
            'updatedAt': None, 'sourceBatchId': None})

    def stage_summary_history(self, limit=50):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('阶段总结历史 limit 必须为 1～100 的整数')
        version = self.stage_summaries()['version']
        return [item for number in range(version, max(0, version-limit), -1)
                if (item := self.store.get('stage-summaries-version:' + str(number))) is not None]

    def _prepare_stage_summary_update(self, data, request_id, source_batch=None, max_chars=None):
        if not isinstance(data, dict) or set(data) != {'expectedVersion', 'selfUnderstanding', 'ownerUnderstanding'}:
            raise ValueError('阶段总结需要 expectedVersion、selfUnderstanding、ownerUnderstanding')
        expected = data['expectedVersion']
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError('阶段总结 expectedVersion 必须为非负整数')
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 300:
            raise ValueError('阶段总结 requestId 必须为非空字符串，最多300字符')
        candidate = {key: data[key] for key in ('expectedVersion', 'selfUnderstanding', 'ownerUnderstanding')}
        for key in ('selfUnderstanding', 'ownerUnderstanding'):
            if not isinstance(candidate[key], str) or not candidate[key].strip():
                raise ValueError('两份阶段总结都必须是完整的非空正文；未完成内容不覆盖有效版本')
            try: candidate[key].encode('utf-8')
            except UnicodeEncodeError as error: raise ValueError('阶段总结必须为有效 Unicode 文本') from error
        digest = hashlib.sha256(encode(dict(candidate, sourceBatchId=source_batch)).encode('utf-8')).hexdigest()
        operation_key = 'stage-summary-request:' + hashlib.sha256(request_id.encode('utf-8')).hexdigest()
        previous = self.store.get(operation_key)
        if previous is not None:
            if previous['fingerprint'] != digest:
                raise ValueError('阶段总结 requestId 已用于不同内容')
            return operation_key, digest, candidate, previous['result']
        limit = self.config['stageSummaryMaxChars'] if max_chars is None else max_chars
        if isinstance(limit, bool) or not isinstance(limit, int) or not 100 <= limit <= 100000:
            raise ValueError('阶段总结容量配置无效')
        if any(len(candidate[key]) > limit for key in ('selfUnderstanding', 'ownerUnderstanding')):
            raise ValueError('阶段总结正文超过配置容量')
        if expected != self.stage_summaries()['version']:
            raise ValueError('阶段总结版本冲突，请重新读取有效版本')
        return operation_key, digest, candidate, None

    def _commit_stage_summaries(self, data, request_id, source_batch=None, max_chars=None):
        # Caller owns the Core transaction so summary, history, receipt and the
        # dream checkpoint commit together. Library publication happens first.
        key, digest, candidate, previous = self._prepare_stage_summary_update(data, request_id, source_batch, max_chars)
        if previous is not None:
            return previous
        snapshot = {name: candidate[name] for name in ('selfUnderstanding', 'ownerUnderstanding')}
        snapshot.update(version=candidate['expectedVersion'] + 1, updatedAt=self.clock(), sourceBatchId=source_batch)
        self.store.put('stage-summaries', snapshot)
        self.store.put('stage-summaries-version:' + str(snapshot['version']), snapshot)
        self.store.put(key, {'fingerprint': digest, 'result': snapshot})
        self.log('stage_summaries.updated', {'version': snapshot['version'], 'sourceBatchId': source_batch})
        return snapshot

    def update_stage_summaries(self, data, request_id=None):
        """Human edits are version checked; retries reuse the same request ID."""
        request_id = 'stage-summary-' + uuid.uuid4().hex if request_id is None else request_id
        with self.store.tx():
            return self._commit_stage_summaries(data, request_id)

    def _dream_summary_update(self, payload, result):
        if 'stageSummaries' not in result:
            return None
        content = result['stageSummaries']
        if not isinstance(content, dict) or set(content) != {'selfUnderstanding', 'ownerUnderstanding'}:
            raise ValueError('做梦 stageSummaries 需要两份完整正文')
        current = payload.get('currentStageSummaries', {'version': 0})
        return dict(content, expectedVersion=current['version'])
