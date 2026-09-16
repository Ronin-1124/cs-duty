import argparse
import sys


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'restore-data':
        parser = argparse.ArgumentParser(description='从迁移包恢复到新的数据目录，不覆盖已有数据')
        parser.add_argument('archive')
        parser.add_argument('--data-dir', required=True)
        args = parser.parse_args(argv[1:])
        from cs_duty.data_management import restore_workspace
        import sqlite3
        try:
            result = restore_workspace(args.archive, args.data_dir)
        except (ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except sqlite3.Error:
            print('迁移数据库无法读取，请检查迁移包是否完整。', file=sys.stderr)
            return 1
        print('已恢复到：' + result['directory'])
        print('启动：.\\run.cmd serve --data-dir "' + result['directory'] + '"')
        if not result['includes_secrets']:
            print('请在管理页重新填写模型密钥和飞书通知配置。')
        print('京麦浏览器需要重新登录。')
        return 0
    if argv and argv[0] == 'import-knowledge':
        parser = argparse.ArgumentParser(description='导入整理后的 Radxa 知识包（请先停止服务）')
        parser.add_argument('bundle')
        parser.add_argument('--data-dir', default='artifacts/app')
        args = parser.parse_args(argv[1:])
        import json
        from pathlib import Path
        from cs_duty.database import Database
        from cs_duty.knowledge_bundle import import_bundle
        db = Database(Path(args.data_dir) / 'business.sqlite3')
        try:
            print(json.dumps(import_bundle(db, Path(args.bundle)), ensure_ascii=False, indent=2))
        finally:
            db.close()
        return 0
    if argv and argv[0] not in ('serve', '-h', '--help'):
        from mock_dongdong.cli import main as replica_cli
        return replica_cli(argv)
    parser = argparse.ArgumentParser(description='本地客服应用：管理页面、LangGraph 流程与网页自动化')
    parser.add_argument('command', nargs='?', default='serve', choices=['serve'])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18766)
    parser.add_argument('--data-dir', default=None)
    args = parser.parse_args(argv)
    from cs_duty.server import serve
    return serve(args.host, args.port, args.data_dir)


if __name__ == '__main__':
    raise SystemExit(main())
