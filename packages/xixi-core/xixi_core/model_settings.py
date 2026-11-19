"""Four public purposes projected onto the existing Core model configuration.

No model key, native DSH default or retired management-console choice is imported.
The config version is the conservative compare-and-swap revision.
"""
import copy
import hashlib
import json

PURPOSES = ('daily', 'work', 'consolidation', 'leisure')
LEGACY_KEYS = {'daily': 'daily', 'work': 'work', 'consolidation': 'maintenance'}
AGENT_PRESETS = frozenset({'standard', 'minimal'})


def bounded_id(value, label, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(label + '需要非空、无控制字符且长度合理的标识')
    return value


def normalize_binding(value):
    if not isinstance(value, dict) or set(value) - {'provider', 'model', 'reasoningEffort', 'agentPreset', 'maxTokens'}:
        raise ValueError('模型绑定字段无效')
    if 'model' not in value:
        raise ValueError('模型绑定需要 model')
    provider = value.get('provider')
    if provider is not None:
        bounded_id(provider, 'provider', 180)
    effort = value.get('reasoningEffort')
    if effort is not None:
        bounded_id(effort, 'reasoningEffort', 100)
    preset = value.get('agentPreset', 'standard')
    if not isinstance(preset, str) or preset not in AGENT_PRESETS:
        raise ValueError('agentPreset只支持standard/minimal')
    result = {'provider': provider, 'model': bounded_id(value['model'], 'model'),
              'reasoningEffort': effort, 'agentPreset': preset}
    if 'maxTokens' in value:
        maximum = value['maxTokens']
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 1000000:
            raise ValueError('maxTokens必须为1～1000000整数')
        result['maxTokens'] = maximum
    return result


def frozen_model_options(config, key):
    options = copy.deepcopy(config.get('modelOptions', {}).get(key, {}))
    options.setdefault('agentPreset', 'standard')
    return options


def binding_from_config(config, key):
    options = frozen_model_options(config, key)
    value = {'provider': options.get('provider'), 'model': config['models'][key],
             'reasoningEffort': options.get('reasoningEffort'), 'agentPreset': options['agentPreset']}
    if 'maxTokens' in options:
        value['maxTokens'] = options['maxTokens']
    return normalize_binding(value)


def binding_options(binding):
    return {key: copy.deepcopy(binding[key]) for key in ('provider', 'reasoningEffort', 'agentPreset', 'maxTokens')
            if key in binding and binding[key] is not None}


def validate_catalog_choice(catalog, binding):
    if not isinstance(catalog, dict) or not isinstance(catalog.get('groups'), list):
        raise ValueError('DSH模型目录无效；未保存新选择')
    provider = binding['provider']
    if provider is None:
        provider = (catalog.get('default') or {}).get('provider')
    if not isinstance(provider, str) or provider not in catalog.get('routableProviders', []):
        raise ValueError('所选或继承的provider不在DSH可路由目录中；未保存新选择')
    group = next((g for g in catalog['groups'] if isinstance(g, dict) and g.get('id') == provider), None)
    model = next((m for m in (group or {}).get('models', []) if isinstance(m, dict) and m.get('id') == binding['model']), None)
    if model is None:
        raise ValueError('所选model不属于该DSH provider；未保存新选择')
    effort = binding['reasoningEffort']
    efforts = (model.get('reasoning') or {}).get('efforts', [])
    effort_ids = {e.get('id') if isinstance(e, dict) else e for e in efforts if isinstance(e, (dict, str))}
    if effort is not None and effort not in effort_ids:
        raise ValueError('reasoningEffort不在所选模型的DSH目录中；未保存新选择')
    presets = catalog.get('agentPresets', [])
    if not any(isinstance(p, dict) and p.get('id') == binding['agentPreset'] and p.get('available') is True for p in presets):
        raise ValueError('所选agentPreset尚未由DSH确认可用；未保存新选择')


class ModelSettingsMixin:
    def model_settings(self):
        with self.config_lock, self.store.lock:
            snapshot = self.configuration()
            return self._model_settings_view(snapshot['config'], snapshot['version'])

    def _model_settings_view(self, config, revision):
        bindings = {purpose: binding_from_config(config, key) for purpose, key in LEGACY_KEYS.items()}
        bindings['leisure'] = copy.deepcopy(config.get('modelSettings', {}).get('leisure'))
        for binding in bindings.values():
            if binding is not None:
                binding['inherited'] = binding['provider'] is None
        prior = self.store.get('model-settings-legacy-snapshot')
        return {'revision': revision, 'bindings': bindings,
                'mapping': {'consolidation': 'maintenance', 'leisure': None},
                'unmergedDream': {'model': config['models']['dream'],
                                  'modelOptions': copy.deepcopy(config['modelOptions']['dream']),
                                  'readOnly': True, 'reason': '整理仅映射maintenance；做梦维持原选择，尚未合并。'},
                'legacyPreserved': {'available': prior is not None, 'revision': prior.get('revision') if prior else None, 'readOnly': True},
                'effects': {'daily': 'new_session', 'work': 'new_task', 'consolidation': 'new_maintenance_batch', 'leisure': 'stored_only_no_trigger'}}

    def update_model_settings(self, data, request_id=None):
        from .config import validate
        if not isinstance(data, dict) or set(data) - {'bindings', 'expectedRevision'}:
            raise ValueError('模型设置需要bindings和expectedRevision')
        expected = data.get('expectedRevision')
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise ValueError('expectedRevision必须为当前正整数版本')
        bounded_id(request_id, 'requestId', 300)
        supplied = data.get('bindings')
        if not isinstance(supplied, dict) or not supplied or set(supplied) - set(PURPOSES):
            raise ValueError('bindings只支持daily/work/consolidation/leisure，且至少提供一项')
        wanted = {}
        for purpose, value in supplied.items():
            if purpose == 'leisure' and value is None:
                wanted[purpose] = None
            else:
                wanted[purpose] = normalize_binding(value)
        fingerprint = hashlib.sha256(json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        receipt_key = 'model-settings-request:' + hashlib.sha256(request_id.encode()).hexdigest()
        def replay():
            prior = self.store.get(receipt_key)
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ValueError('相同requestId的模型设置内容冲突')
                return copy.deepcopy(prior['result'])
            return None
        with self.config_lock, self.store.lock:
            prior = replay()
            if prior is not None:
                return prior
            snapshot = self.configuration()
            if snapshot['version'] != expected:
                raise ValueError('模型设置版本冲突，请重新读取')
            before = snapshot['config']
            existing = {purpose: binding_from_config(before, key) for purpose, key in LEGACY_KEYS.items()}
            existing['leisure'] = copy.deepcopy(before.get('modelSettings', {}).get('leisure'))
            changed = {purpose: binding for purpose, binding in wanted.items() if binding != existing[purpose]}
        if any(binding is not None for binding in changed.values()):
            catalog_method = getattr(self.runtime, 'model_catalog', None)
            if not callable(catalog_method):
                raise ValueError('DSH模型目录不可用；未保存新选择')
            try:
                catalog = catalog_method()
            except Exception as error:
                raise ValueError('DSH模型目录读取失败；未保存新选择') from error
            for binding in changed.values():
                if binding is not None:
                    validate_catalog_choice(catalog, binding)
        with self.config_lock:
            with self.store.tx():
                prior = replay()
                if prior is not None:
                    return prior
                revision = self.store.get('config_version', 1)
                if revision != expected:
                    raise ValueError('模型设置版本冲突，请重新读取')
                config = copy.deepcopy(self.config)
                for purpose, binding in changed.items():
                    if purpose == 'leisure':
                        config.setdefault('modelSettings', {})['leisure'] = copy.deepcopy(binding)
                    else:
                        key = LEGACY_KEYS[purpose]
                        config['models'][key] = binding['model']
                        config['modelOptions'][key] = binding_options(binding)
                config = validate(config)
                if changed:
                    if self.store.get('model-settings-legacy-snapshot') is None:
                        self.store.put('model-settings-legacy-snapshot', {'revision': revision,
                            'models': copy.deepcopy(self.config['models']), 'modelOptions': copy.deepcopy(self.config['modelOptions']),
                            'modelSettings': copy.deepcopy(self.config.get('modelSettings', {'leisure': None}))})
                    revision += 1
                    self.store.put('config', config)
                    self.store.put('config_version', revision)
                    self.store.put('config_version:' + str(revision), config)
                    self.log('model_settings.changed', {'revision': revision, 'purposes': sorted(changed)})
                result = self._model_settings_view(config, revision)
                self.store.put(receipt_key, {'fingerprint': fingerprint, 'result': result})
            self.config = copy.deepcopy(config)
        return result
