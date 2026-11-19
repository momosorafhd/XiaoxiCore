"""Protocol capacity limits, measured after serialization where appropriate."""
import json

MAX_INPUT_CHARS = 200000
MAX_HTTP_BODY_BYTES = 2 * 1024 * 1024
MAX_SYSTEM_EVENT_CHARS = 12000
TASK_SUMMARY_PREVIEW_CHARS = 4000
TASK_ARTIFACT_PREVIEW_COUNT = 5
TASK_ARTIFACT_PREVIEW_CHARS = 1024
DEFAULT_RESULT_PAGE_CHARS = 4096
MAX_RESULT_PAGE_CHARS = 16000


def canonical_json(value):
    # ASCII JSON has equal character/UTF-8 byte lengths, preserves every Unicode
    # codepoint including escaped surrogate data, and rejects non-JSON NaN/Inf.
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(',', ':'))


def result_notice(task_id, result, result_ref, digest, total_chars, budget=MAX_SYSTEM_EVENT_CHARS):
    """A bounded notification, never a replacement for the full stored result."""
    if not isinstance(budget, int) or isinstance(budget, bool) or not 512 <= budget <= MAX_INPUT_CHARS:
        raise ValueError('系统输入预算无效')
    original = result['summary']
    references = result.get('artifactRefs', [])
    refs = [ref for ref in references[:TASK_ARTIFACT_PREVIEW_COUNT]
            if len(ref) <= TASK_ARTIFACT_PREVIEW_CHARS]
    preview = original[:TASK_SUMMARY_PREVIEW_CHARS]

    def render(count):
        return canonical_json({'taskId': task_id, 'status': result['status'], 'summary': preview[:count],
                               'artifactRefs': refs, 'resultRef': result_ref, 'sha256': digest,
                               'totalChars': total_chars, 'summaryTruncated': count < len(original),
                               'artifactRefsTruncated': len(refs) < len(references), 'artifactCount': len(references)})

    # References are indivisible identifiers: omit whole references that cannot
    # fit, rather than silently turning them into invalid truncated URIs.
    while refs and len(render(0)) > budget:
        refs.pop()
    if len(render(0)) > budget:
        raise ValueError('任务结果引用元数据超过系统输入预算')
    low, high = 0, len(preview)
    while low < high:
        middle = (low + high + 1) // 2
        if len(render(middle)) <= budget:
            low = middle
        else:
            high = middle - 1
    text = render(low)
    if len(text) > budget:
        raise ValueError('系统输入序列化超限')
    return text
