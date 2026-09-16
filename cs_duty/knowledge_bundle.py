"""Import a traceable cleaning delivery without promoting candidate claims to facts."""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path

FILES = ('csv_records', 'official_documents', 'knowledge_candidates', 'policy_candidates',
         'product_aliases', 'review_queue')


def read_bundle(root):
    manifest = json.loads((root / 'source_manifest.json').read_text(encoding='utf-8'))
    data, hashes = {}, {}
    for name in FILES:
        raw = (root / (name + '.jsonl')).read_bytes()
        hashes[name] = hashlib.sha256(raw).hexdigest()
        rows = [json.loads(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip()]
        if any(not isinstance(r, dict) or not r.get('id') or r.get('schema') != 'radxa-kcs-1.0' for r in rows):
            raise ValueError(f'{name}：记录缺少 ID 或格式版本不受支持')
        if len({r['id'] for r in rows}) != len(rows):
            raise ValueError(f'{name}：重复 ID')
        data[name] = rows
    expected = {(f['path'], n) for f in manifest['csv_inputs'] for n in range(2, f['logical_records'] + 2)}
    actual = [(r['source_file'], r['source_row']) for r in data['csv_records']]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError('CSV 行追溯不完整或重复')
    for doc in data['official_documents']:
        if doc['source_commit'] != manifest['official_commit']:
            raise ValueError('官方文档提交与清单不一致')
    for name in ('source_manifest.json', 'report.md', 'style_guide.md'):
        hashes[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    return manifest, data, hashes


def sections(body):
    """Split only at top-level sections, never inside tables, code, tabs or admonitions."""
    blocks, current, fence, tabs, admonition = [], [], None, 0, False
    for line in body.splitlines():
        stripped = line.lstrip()
        marker = re.match(r'(`{3,}|~{3,})', stripped)
        if marker:
            char = marker[1][0]
            fence = None if fence == char else (fence or char)
        if not fence:
            if stripped.startswith(':::'):
                admonition = stripped != ':::'
            if re.match(r'^## ', line) and not tabs and not admonition and current:
                blocks.append('\n'.join(current).strip())
                current = []
            tabs += len(re.findall(r'<(?:Tabs|table)(?:\s[^>]*|)>', line))
            tabs -= len(re.findall(r'</(?:Tabs|table)>', line))
            tabs = max(0, tabs)
        current.append(line)
    if current:
        blocks.append('\n'.join(current).strip())
    intro = blocks[0] if blocks and not blocks[0].startswith('## ') else ''
    for index, block in enumerate(blocks):
        if not block:
            continue
        # Keep page-wide prerequisites alongside each section, rather than truncating them.
        text = (intro + '\n\n' + block) if index and intro else block
        yield index, block.splitlines()[0].lstrip('# ').strip(), text


def import_bundle(db, root: Path):
    manifest, data, hashes = read_bundle(root)
    batch = hashlib.sha256(('importer-v1:' + json.dumps(hashes, sort_keys=True)).encode()).hexdigest()
    previous = db.setting('knowledge_bundle', {})
    if previous.get('batch') == batch:
        return {**previous, 'unchanged': True}
    aliases = []
    scopes = {p for d in data['official_documents'] for p in d['product_scope']}
    for product in sorted(scopes):
        aliases.append({'alias': product, 'products': [product]})
        aliases.append({'alias': product.replace('-', ' ').replace(' plus', '+'), 'products': [product]})
        aliases.append({'alias': product.replace('-plus', '+').replace('-', ''), 'products': [product]})
    for entry in data['product_aliases']:
        if entry.get('discarded') or entry['unresolved'] or entry['ambiguous'] or not entry['canonical_ids']:
            continue
        for alias in [entry['raw'], *entry['canonical_names']]:
            aliases.append({'alias': alias, 'products': entry['canonical_ids']})

    eligible, reasons = {}, {}
    for doc in data['official_documents']:
        reason = ''
        if not doc['is_standalone_fact_source'] or doc['doc_kind'] not in ('page', 'wrapper'):
            reason = '非独立产品正文'
        elif doc['parse_issues']:
            reason = '存在未解决的解析问题'
        elif not doc['body'].strip():
            reason = '正文为空'
        if reason:
            reasons[doc['id']] = reason
        else:
            # Prefer Chinese; English is a fallback for that page, with language preserved.
            path = doc['source_path'].replace('i18n/en/docusaurus-plugin-content-docs/current/', 'docs/')
            if path not in eligible or doc['language'] == 'zh':
                eligible[path] = doc

    questions = {}
    for candidate in data['knowledge_candidates']:
        if candidate['verification_status'] == 'checked_supported':
            for source in candidate['sources']:
                if source.get('kind') == 'official':
                    questions.setdefault(source['id'], []).extend(candidate.get('original_questions', []))
    entries, active_docs, oversized = [], set(), 0
    for doc in eligible.values():
        source = {'kind': 'official', 'file': doc['source_path'], 'commit': doc['source_commit'],
                  'url': doc['public_url'], 'language': doc['language'], 'products': doc['product_scope'],
                  'document_id': doc['id'], 'dependencies': doc['resolved_dependencies']}
        scope_note = '文档路径覆盖产品：' + ', '.join(doc['product_scope'])
        related = list(dict.fromkeys(questions.get(doc['id'], [])))
        for index, heading, body in sections(doc['body']):
            content = scope_note + '\n资料说明：图片未读取；共享页面中的变体、系统版本与配件条件不能互相替代。\n\n' + body
            if len(content) > 12000:
                oversized += 1
                continue
            if len(re.sub(r'[#\s]', '', body)) < 30:
                continue
            source_id = 'official:' + hashlib.sha256(f"{doc['id']}:{index}".encode()).hexdigest()
            title = ' / '.join([*doc['product_scope'], doc['title'], heading])
            if related:
                title += ' / 相关问法（非结论）：' + '；'.join(related)[:500]
            entries.append((source_id, title, content, ' '.join(doc['product_scope']),
                            json.dumps([{**source, 'section': heading}], ensure_ascii=False), time.time()))
            active_docs.add(doc['id'])
    if not entries:
        raise ValueError('整理包没有可独立使用的官方正文，未替换现有知识')

    counts = {name: len(rows) for name, rows in data.items()}
    summary = {'batch': batch, 'file': 'knowledge-cleaned', 'official_commit': manifest['official_commit'],
               'counts': counts, 'inserted': len(entries), 'active_documents': len(active_docs),
               'held_documents': len(data['official_documents']) - len(active_docs),
               'oversized_sections': oversized, 'candidate_statuses': dict(Counter(
                   r['verification_status'] for r in data['knowledge_candidates'])), 'hashes': hashes,
               'imported_at': time.time(), 'duplicates': 0,
               'note': '候选问答和店铺规则保留供审核；回复依据为完整官方章节，不把核对标签直接当事实。'}
    with db.lock, db.conn:
        disabled = {r['id'] for r in db.conn.execute("SELECT id FROM knowledge WHERE enabled=0 AND id LIKE 'official:%'")}
        # Preserve manual entries and their on/off switches; retire the old bulk CSV import.
        for row in db.conn.execute('SELECT id,sources FROM knowledge').fetchall():
            sources = json.loads(row['sources'])
            if any(s.get('kind') == 'official' or s.get('file') in {f['path'] for f in manifest['csv_inputs']} for s in sources):
                db.conn.execute('UPDATE knowledge SET enabled=0 WHERE id=?', (row['id'],))
        for kid, title, content, product, sources, updated in entries:
            enabled = 0 if kid in disabled else 1
            db.conn.execute('INSERT INTO knowledge VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                            'title=excluded.title,content=excluded.content,product=excluded.product,'
                            'sources=excluded.sources,enabled=excluded.enabled,updated=excluded.updated',
                            (kid, title, content, product, sources, enabled, updated))
        db.conn.execute('DELETE FROM knowledge_materials')
        for name, rows in data.items():
            for row in rows:
                if name == 'official_documents':
                    status = 'active' if row['id'] in active_docs else reasons.get(row['id'], '备用语言或需章节复核')
                else:
                    status = 'discarded' if row.get('discarded') else row.get('verification_status', row.get('status', 'reference'))
                db.conn.execute('INSERT INTO knowledge_materials VALUES(?,?,?,?,?)',
                                (row['id'], name, status, json.dumps(row, ensure_ascii=False), batch))
        for name in ('source_manifest.json', 'report.md', 'style_guide.md'):
            db.conn.execute('INSERT INTO knowledge_materials VALUES(?,?,?,?,?)',
                            (name, 'delivery_notes', 'reference', json.dumps({'text': (root / name).read_text(encoding='utf-8')}, ensure_ascii=False), batch))
        # Use SQL inside this transaction so a failed import cannot partially replace the library.
        for key, value in [('knowledge_aliases', aliases), ('knowledge_bundle', summary)]:
            db.conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))
    return summary
