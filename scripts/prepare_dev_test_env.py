"""RC.7 dev 隔离测试环境准备（一次性脚本）。

仅生成合成 AI 使用记录，全部写入 D:\\zz-dev-test（测试专用目录）；
不读取真实 AI 客户端来源或生产账本，不含任何用户名/密钥/个人数据。
导入本模块不执行任何操作（副作用只在 __main__ 下发生）。
"""
import json, os, zstandard


def main():
    fx = r'D:\zz-dev-test\Fixtures'
    dr = r'D:\zz-dev-test\DataRoot'
    test_root = r'D:\zz-dev-test'
    la_fwd = test_root.replace(os.sep, '/')  # fixtures 路径前缀

    os.makedirs(os.path.join(fx, 'wb'), exist_ok=True)
    os.makedirs(os.path.join(fx, 'codex'), exist_ok=True)
    os.makedirs(os.path.join(fx, 'dsh-home', 'sessions', 'p', 's1'), exist_ok=True)
    os.makedirs(os.path.join(fx, 'cp'), exist_ok=True)
    os.makedirs(dr, exist_ok=True)

    # WorkBuddy (3 token events)
    with open(os.path.join(fx, 'wb', 'sess-1.jsonl'), 'w', encoding='utf-8') as f:
        for i in range(3):
            f.write(json.dumps({'id': f'wb-{i}', 'sessionId': 'wb-sess',
                'timestamp': 1787000000000 + i * 1000, 'type': 'function_call',
                'providerData': {'model': 'glm-test-model', 'rawUsage': {
                    'prompt_tokens': 1300, 'completion_tokens': 300,
                    'prompt_tokens_details': {'cached_tokens': 300}}}}) + '\n')

    # Codex (1 token event)
    with open(os.path.join(fx, 'codex', 'rollout-1.jsonl'), 'w', encoding='utf-8') as f:
        f.write(json.dumps({'type': 'session_meta', 'payload': {
            'session_id': 'cx-1',
            'cwd': os.path.join(fx, 'cx-proj').replace(os.sep, '/')}}) + '\n')
        f.write(json.dumps({'type': 'token_usage_record',
            'timestamp': '2026-08-01T10:00:00.000Z', 'ordinal': 1,
            'payload': {'response_id': 'r1', 'usage': {
                'input_tokens': 1000, 'output_tokens': 100}}}) + '\n')

    # dsh (1 token event, real zstd)
    dd = os.path.join(fx, 'dsh-home', 'sessions', 'p', 's1')
    lines = [
        json.dumps({'type': 'session', 'id': 'dsh-1',
                    'cwd': os.path.join(fx, 'dsh-proj').replace(os.sep, '/')}),
        json.dumps({'type': 'assistant/message', 'seq': 1, 'time': 1787000000000,
                    'data': {'usage': {'inputTokens': 500, 'outputTokens': 50,
                                       'reasoningTokens': 0, 'cacheReadTokens': 0,
                                       'cacheWriteTokens': 0},
                             'message': {'source': {'model': 'deepseek-chat',
                                                    'provider': 'deepseek'}}}})]
    with open(os.path.join(dd, 'session.jsonl.zstd'), 'wb') as f:
        f.write(zstandard.ZstdCompressor().compress(
            ''.join(l + '\n' for l in lines).encode('utf-8')))

    # CatPaw (4 activity events)
    with open(os.path.join(fx, 'cp', 'conv.jsonl'), 'w', encoding='utf-8') as f:
        for i in range(4):
            f.write(json.dumps({'messageId': f'cp-{i}', 'conversationId': 'cp-conv',
                'type': 'user' if i % 2 == 0 else 'assistant',
                'timestamp': f'2026-08-01T10:0{i}:00.000Z'}) + '\n')

    # sources.json（无 BOM，路径指向 Fixtures）
    la = test_root.replace(os.sep, '/')  # 指向测试根（Fixtures 的父目录）
    sources = {
        '_note': 'RC.7 dev 隔离测试（合成来源，USAGE_LEDGER_HOME 隔离）',
        'zcode':     la + '/none-zcode.sqlite',
        'dsh':       la + '/Fixtures/dsh-home',
        'workbuddy': la + '/Fixtures/wb',
        'catpaw':    la + '/Fixtures/cp',
        'traecn':    la + '/none-trae',
        'codex':     la + '/Fixtures/codex',
    }
    with open(os.path.join(dr, 'sources.json'), 'w', encoding='utf-8') as f:
        json.dump(sources, f, ensure_ascii=False, indent=2)

    print('test env ready:')
    for root, dirs, files in os.walk(fx):
        for fn in sorted(files):
            print(' ', os.path.relpath(os.path.join(root, fn), fx))
    print('sources.json:', os.path.join(dr, 'sources.json'))


if __name__ == '__main__':
    main()
