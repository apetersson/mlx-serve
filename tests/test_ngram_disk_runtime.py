#!/usr/bin/env python3
"""Exercise disk-only Qwen inference, SSD restores, text HTTP SSE and Ollama."""
import argparse
import hashlib
import json
import pathlib
import time
import urllib.request

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--url', default='http://127.0.0.1:10888')
p.add_argument('--upstream-url', default='http://127.0.0.1:18080')
p.add_argument('--model', required=True)
p.add_argument('--api-key-file')
p.add_argument('--long-tokens', type=int, default=90000)
p.add_argument('--out', required=True)
p.add_argument('--cache-root', help='Wait for the durable long-prefix manifest before checking SSD restore')
a = p.parse_args()
key = pathlib.Path(a.api_key_file).read_text().strip() if a.api_key_file else ''
results = []


def call(base, route, body=None):
    headers = {'Content-Type': 'application/json'}
    if base == a.url and key:
        headers['Authorization'] = 'Bearer ' + key
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + route, data=data, headers=headers)
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=1800) as response:
        raw = response.read()
        return raw, {k.lower(): v for k, v in response.headers.items()}, time.monotonic() - start


def js(base, route, body=None):
    raw, headers, elapsed = call(base, route, body)
    result = json.loads(raw)
    assert 'error' not in result, result.get('error')
    return result, headers, elapsed


def record(name, **details):
    row = dict(name=name, **details)
    results.append(row)
    pathlib.Path(a.out).write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(row), flush=True)


# The upstream itself must frame SSE; a gateway can mask a missing upstream trailer.
for base in (a.upstream_url, a.url):
    model = a.model if base == a.url else 'default'
    for route, body, terminal in (
        ('/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'Say ready.'}], 'max_tokens': 16}, '[DONE]'),
        ('/v1/completions', {'prompt': 'Answer briefly: 2 + 2 =', 'max_tokens': 16}, '[DONE]'),
        ('/v1/messages', {'messages': [{'role': 'user', 'content': 'Say ready.'}], 'max_tokens': 16}, 'message_stop'),
        ('/v1/responses', {'input': 'Say ready.', 'max_output_tokens': 16}, 'response.completed'),
    ):
        body.update(model=model, stream=True, temperature=0, enable_thinking=False, enable_mtp=False)
        raw, headers, elapsed = call(base, route, body)
        assert headers.get('transfer-encoding', '').lower() == 'chunked', headers
        text = raw.decode()
        assert terminal in text or (terminal == 'response.completed' and 'response.incomplete' in text), text[-1000:]
        events = []
        for line in text.splitlines():
            if line.startswith('data:') and line[5:].strip() != '[DONE]':
                event = json.loads(line[5:].strip())
                assert 'error' not in event and event.get('type') not in ('error', 'response.failed'), event
                events.append(event)
        assert events, text
        record('stream', url=base, route=route, seconds=round(elapsed, 3), events=len(events), terminal=terminal)

body = {'model': 'default', 'messages': [{'role': 'user', 'content': 'Say ready.'}], 'stream': True,
        'options': {'num_predict': 16, 'temperature': 0}}
raw, headers, elapsed = call(a.upstream_url, '/api/chat', body)
lines = [json.loads(line) for line in raw.splitlines() if line]
assert lines and lines[-1].get('done') is True, lines[-1:]
assert not any('error' in line for line in lines), lines
record('ollama', seconds=round(elapsed, 3), lines=len(lines))

before, _, _ = js(a.upstream_url, '/props')
assert before['settings']['prefix_cache']['ram_enabled'] is False, before['settings']['prefix_cache']
assert not before.get('ngram_warm'), before.get('ngram_warm')
record('props_before', settings=before['settings'], memory=before['memory'])

# Vary record ids/checksums so this is not just a repeated-row table-cache microbenchmark.
if a.long_tokens:
    records = [f'Record {i}: checksum {hashlib.sha256(str(i).encode()).hexdigest()[:24]}; status verified.\n'
               for i in range(a.long_tokens // 8 + 100)]
    prefix = 'Read these records. The verification word is disk-ready.\n'
    suffix = '\nReply with only the verification word: disk-ready.'
    tokenized, _, _ = js(a.upstream_url, '/tokenize', {'content': prefix + ''.join(records[:100]) + suffix})
    estimate = max(1, int(a.long_tokens * 100 / len(tokenized['tokens'])))
    for _ in range(8):
        prompt = prefix + ''.join(records[:estimate]) + suffix
        tokenized, _, _ = js(a.upstream_url, '/tokenize', {'content': prompt})
        count = len(tokenized['tokens'])
        if a.long_tokens - 300 <= count <= a.long_tokens:
            break
        estimate = max(1, int(estimate * (a.long_tokens - 100) / count))
    assert a.long_tokens - 300 <= count <= a.long_tokens, count
    body = {'model': a.model, 'messages': [{'role': 'user', 'content': prompt}], 'stream': False,
            'max_tokens': 32768, 'temperature': 0, 'enable_thinking': False, 'enable_mtp': False}
    wall_start = time.time()
    cold, _, elapsed = js(a.url, '/v1/chat/completions', body)
    usage = cold['usage']
    assert usage['prompt_tokens'] >= a.long_tokens - 300, usage
    # Model wording is diagnostic, not a storage invariant.
    assert cold['choices'][0]['message'].get('content', ''), cold['choices'][0]
    record('long_cold', seconds=round(elapsed, 3), usage=usage, reply=cold['choices'][0]['message']['content'])
    if a.cache_root:
        deadline = time.monotonic() + 120
        durable = None
        while time.monotonic() < deadline:
            for path in pathlib.Path(a.cache_root).glob('*/*/meta.json'):
                try:
                    if path.stat().st_mtime < wall_start:
                        continue
                    meta = json.loads(path.read_text())
                    if meta.get('tokens', 0) >= usage['prompt_tokens']:
                        durable = str(path)
                        break
                except (OSError, ValueError):
                    continue
            if durable:
                break
            time.sleep(0.1)
        assert durable, 'No durable long-prefix manifest landed within 120 seconds'
        record('ssd_manifest_durable', path=durable)
    warm, _, elapsed = js(a.url, '/v1/chat/completions', body)
    assert warm['usage']['prompt_tokens_details']['cached_tokens'] > count // 2, warm['usage']
    assert warm['choices'][0]['message'].get('content', ''), warm['choices'][0]
    record('long_ssd_restore', seconds=round(elapsed, 3), usage=warm['usage'], reply=warm['choices'][0]['message']['content'])

# A completed large prefix must not remain in MLX RAM between turns.
after, _, _ = js(a.upstream_url, '/props')
assert after['settings']['prefix_cache']['ram_enabled'] is False
assert after['memory']['kv_cache_bytes'] < 128 * 1024 * 1024, after['memory']
assert not after.get('ngram_warm')
record('props_after', settings=after['settings'], memory=after['memory'])
print('PASS', flush=True)
