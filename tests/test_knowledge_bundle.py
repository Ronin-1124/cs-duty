import json
import tempfile
import unittest
from pathlib import Path

from cs_duty.database import Database
from cs_duty.knowledge import Knowledge
from cs_duty.knowledge_bundle import FILES, import_bundle, sections


class KnowledgeBundleCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.db = Database(self.root / 'db.sqlite3')
        self.addCleanup(self.db.close)
        self.knowledge = Knowledge(self.db)
        self.knowledge.import_csv('old.csv', '产品型号,问题,答案\n5B,旧库存,今天有货\n')
        self.data = {name: [] for name in FILES}
        self.data['csv_records'] = [{'id': 'csv:1', 'source_file': 'old.csv', 'source_row': 2}]
        self.data['policy_candidates'] = [{'id': 'policy:1', 'rule_verbatim': '今天有货'}]
        self.data['product_aliases'] = [
            {'id': 'alias:1', 'raw': '5B', 'canonical_ids': ['rock-5b'], 'canonical_names': ['ROCK 5B'], 'ambiguous': False, 'unresolved': False},
            {'id': 'alias:2', 'raw': '5B+', 'canonical_ids': ['rock-5b-plus'], 'canonical_names': ['ROCK 5B+'], 'ambiguous': False, 'unresolved': False},
        ]
        for variant in ('5b', '5b-plus'):
            self.data['official_documents'].append({'id': 'doc:' + variant, 'source_path': 'docs/' + variant + '.md',
                'source_commit': 'test-commit', 'language': 'zh', 'doc_kind': 'page',
                'is_standalone_fact_source': True, 'parse_issues': [], 'product_scope': ['rock-' + variant],
                'product_scope_note': '未经核对的归纳不应成为正文依据', 'public_url': None,
                'resolved_dependencies': [], 'title': '供电说明',
                'body': '# 供电说明\n\n## 电源要求\n\n型号 ' + variant + '：本页只适用于指定型号，电源电压与接口要求不得套用其他变体。'})
        self.data['official_documents'].append({**self.data['official_documents'][0], 'id': 'doc:bad',
            'source_path': 'docs/bad.md', 'parse_issues': ['unsubstituted_props'], 'body': '错误占位内容'})
        self.write()

    def write(self):
        for name, rows in self.data.items():
            (self.root / (name + '.jsonl')).write_text('\n'.join(json.dumps({**r, 'schema': 'radxa-kcs-1.0'}, ensure_ascii=False) for r in rows), encoding='utf-8')
        (self.root / 'source_manifest.json').write_text(json.dumps({'official_commit': 'test-commit',
            'csv_inputs': [{'path': 'old.csv', 'logical_records': 1}]}), encoding='utf-8')
        for name in ('style_guide.md', 'report.md'):
            (self.root / name).write_text('reference', encoding='utf-8')

    def test_import_retains_materials_but_only_enables_resolved_official_text(self):
        result = import_bundle(self.db, self.root)
        self.assertEqual(result['active_documents'], 2)
        self.assertEqual(self.db.one("SELECT count(*) n FROM knowledge WHERE enabled=1")['n'], 2)
        self.assertEqual(self.knowledge.search('今天有货'), [])
        self.assertEqual(self.knowledge.search('错误占位内容'), [])
        self.assertTrue(self.db.one("SELECT id FROM knowledge_materials WHERE kind='policy_candidates'"))
        self.assertNotIn('未经核对的归纳', self.db.one("SELECT content FROM knowledge WHERE enabled=1")['content'])

    def test_variant_search_and_disable_survive_reimport(self):
        import_bundle(self.db, self.root)
        for query, expected in [('5B 供电', 'rock-5b'), ('5B+ 供电', 'rock-5b-plus'), ('ROCK5B+ 电源', 'rock-5b-plus')]:
            matches = self.knowledge.search(query)
            self.assertTrue(matches, query)
            self.assertTrue(all(r['product'] == expected for r in matches), query)
        kid = self.knowledge.search('5B+ 电源')[0]['id']
        self.db.execute('UPDATE knowledge SET enabled=0 WHERE id=?', (kid,))
        self.assertTrue(import_bundle(self.db, self.root)['unchanged'])
        self.data['policy_candidates'].append({'id': 'policy:2', 'rule_verbatim': '待确认'})
        self.write()
        import_bundle(self.db, self.root)
        self.assertEqual(self.db.one('SELECT enabled FROM knowledge WHERE id=?', (kid,))['enabled'], 0)

    def test_invalid_bundle_does_not_replace_live_knowledge(self):
        self.data['csv_records'] = []
        self.write()
        with self.assertRaises(ValueError):
            import_bundle(self.db, self.root)
        self.assertTrue(self.knowledge.search('旧库存'))

    def test_fts_follows_updates_replacements_and_deletes(self):
        kid = self.db.one('SELECT id FROM knowledge')['id']
        self.db.execute('UPDATE knowledge SET title=?,content=? WHERE id=?', ('散热要求', '需要风扇散热', kid))
        self.assertTrue(self.knowledge.search('风扇散热'))
        self.assertFalse(self.knowledge.search('旧库存'))
        self.db.execute("INSERT OR REPLACE INTO knowledge VALUES(?,?,?,?,?,1,0)", (kid, '显示接口', '支持指定显示接口', '', '[]'))
        self.assertTrue(self.knowledge.search('显示接口'))
        self.assertFalse(self.knowledge.search('风扇散热'))
        self.db.execute('DELETE FROM knowledge WHERE id=?', (kid,))
        self.assertFalse(self.knowledge.search('显示接口'))

    def test_sections_keep_code_tables_and_prerequisites_intact(self):
        body = '# 标题\n必须先断电。\n## 步骤\n```sh\n## not a heading\n```\n<Tabs>\n## 变体\n</Tabs>\n## 参数\n<table>\n<tr><td>5B+</td><td>5V</td></tr>\n</table>'
        chunks = list(sections(body))
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all('必须先断电' in content for _, _, content in chunks))
        self.assertIn('## 变体\n</Tabs>', chunks[1][2])
        self.assertIn('<table>\n<tr><td>5B+</td><td>5V</td></tr>\n</table>', chunks[2][2])


if __name__ == '__main__':
    unittest.main()
