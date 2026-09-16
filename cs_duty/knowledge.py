"""CSV source preservation, repeatable import and lightweight Chinese retrieval."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time


# Keep original provenance names compatible with the curated bundle manifest.
RAW_CSV_SOURCES = {
    'customer-qa.csv': '电商客服Q&A_数据表_表格.csv',
    'quick-phrases.csv': '2026-09-07快捷短语.csv',
    'kefubao-phrases.csv': 'kefubao话术.csv',
}


def terms(text):
    text = text.lower()
    words = re.findall(r'[a-z0-9][a-z0-9_.+-]*', text)
    for segment in re.findall(r'[\u4e00-\u9fff]+', text):
        words.extend(segment[i:i+2] for i in range(len(segment)-1))
    return set(words)


class Knowledge:
    def __init__(self, db):
        self.db = db

    def import_csv(self, filename, text):
        if not filename.lower().endswith('.csv') or len(text) > 4_000_000:
            raise ValueError('请选择 4 MB 以内的 CSV 文件')
        reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
        columns = reader.fieldnames or []
        question = next((c for c in columns if c == '问题'), None)
        answer = next((c for c in columns if c == '答案' or '话术内容' in c or '快捷短语内容' in c), None)
        title = next((c for c in columns if '话术标题' in c), None)
        product_column = next((c for c in columns if c == '产品型号'), None)
        if not answer:
            raise ValueError('未找到“答案”“话术内容”或“快捷短语内容”列')
        inserted, duplicate, empty = 0, 0, 0
        for row_number, row in enumerate(reader, 2):
            content = (row.get(answer) or '').strip()
            if not content:
                empty += 1
                continue
            label = (row.get(question or title) or content[:60]).strip()
            product = (row.get(product_column) or '').strip()
            digest = hashlib.sha256(json.dumps([product, label, content], ensure_ascii=False).encode()).hexdigest()
            source = {'file': filename, 'row': row_number}
            existing = self.db.one('SELECT sources FROM knowledge WHERE id=?', (digest,))
            if existing:
                sources = json.loads(existing['sources'])
                if source not in sources:
                    sources.append(source)
                    self.db.execute('UPDATE knowledge SET sources=? WHERE id=?', (json.dumps(sources, ensure_ascii=False), digest))
                duplicate += 1
            else:
                self.db.execute('INSERT INTO knowledge VALUES(?,?,?,?,?,1,?)', (digest, label, content, product, json.dumps([source], ensure_ascii=False), time.time()))
                inserted += 1
        return {'file': filename, 'inserted': inserted, 'duplicates': duplicate, 'empty': empty}

    def search(self, query, limit=6):
        query_terms = terms(query)
        if not query_terms:
            return []
        candidates = []
        aliases = self.db.setting('knowledge_aliases', [])
        requested = self.products_in(query, aliases)
        product_terms = terms(' '.join(e['alias'] for e in aliases if requested & set(e['products'])))
        topic_terms = query_terms - product_terms or query_terms
        expanded = set(topic_terms)
        for group in ('供电 电源 power', '显示 输出 display hdmi', '散热 heatsink cooling',
                      '启动 boot bootloader', '系统 os ubuntu debian windows android'):
            group_terms = terms(group)
            if topic_terms & group_terms:
                expanded.update(group_terms)
        # FTS narrows candidates before Python scoring; Chinese uses overlapping bigrams.
        match = ' OR '.join('"' + t.replace('"', '""') + '"' for t in sorted(expanded)[:120])
        scope_sql, scope_args = '', []
        if requested:
            scope_sql = " AND (k.id NOT LIKE 'official:%' OR " + ' OR '.join("instr(' '||k.product||' ',?)>0" for _ in requested) + ')'
            scope_args = [' ' + p + ' ' for p in sorted(requested)]
        rows = self.db.rows('SELECT k.*,bm25(knowledge_fts,3,1) AS rank FROM knowledge_fts '
                            'JOIN knowledge k ON k.rowid=knowledge_fts.rowid '
                            'WHERE knowledge_fts MATCH ? AND k.enabled=1' + scope_sql + ' ORDER BY rank LIMIT 600', (match, *scope_args))
        for row in rows:
            sources = json.loads(row['sources'])
            scope = {p for s in sources for p in s.get('products', [])}
            if not scope:
                scope = self.products_in(row['product'], aliases)
            if requested and scope and not requested & scope:
                continue
            title_terms = terms(row['title'] + ' ' + row['product'])
            body_terms = terms(row['content'])
            overlap = expanded & (title_terms | body_terms)
            if not overlap:
                continue
            exact = topic_terms & (title_terms | body_terms)
            score = len(exact & title_terms) * 3 + len(exact) * 2 + len(overlap - exact) * .3
            product_tokens = re.findall(r'[a-z]+[ -]?\d[a-z0-9]*', row['product'].lower())
            requested_tokens = re.findall(r'[a-z]+[ -]?\d[a-z0-9]*', query.lower())
            if not aliases and requested_tokens and product_tokens and not set(requested_tokens) & set(product_tokens):
                continue
            if not requested and len(overlap) < 2 and not any(re.search(r'\d', t) for t in overlap):
                continue
            if requested and scope & requested:
                score += 20
            candidates.append((score, row))
        candidates.sort(key=lambda item: (-item[0], item[1]['rank']))
        return [{'id': r['id'], 'title': r['title'], 'content': r['content'],
                 'product': r['product'], 'sources': json.loads(r['sources'])} for _, r in candidates[:limit]]

    @staticmethod
    def products_in(text, aliases):
        matches = []
        for entry in aliases:
            pattern = re.escape(entry['alias']).replace(r'\ ', r'[\s-]*')
            for match in re.finditer(r'(?<![a-z0-9])' + pattern + r'(?![a-z0-9+])', text, re.I):
                matches.append((match.start(), match.end(), entry['products']))
        selected, covered = set(), set()
        for start, end, products in sorted(matches, key=lambda m: m[1]-m[0], reverse=True):
            span = set(range(start, end))
            if span & covered:
                continue
            selected.update(products)
            covered.update(span)
        return selected
