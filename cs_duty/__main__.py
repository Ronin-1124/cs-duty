"""Command line entry point: serve and offline maintenance commands."""
import argparse
import sys


def build_parser():
    parser = argparse.ArgumentParser(prog='cs_duty',
                                     description='本地客服应用：管理页面、LangGraph 流程与桌面客户端接入')
    commands = parser.add_subparsers(dest='command', metavar='命令')

    serve = commands.add_parser('serve', help='启动本机客服服务（默认命令）',
                                description='启动本机客服服务与管理页面，直到按 Ctrl+C 停止。')
    serve.add_argument('--host', default='127.0.0.1', help='监听地址，仅允许 127.0.0.1/localhost')
    serve.add_argument('--port', type=int, default=18766, help='监听端口（默认 18766）')
    serve.add_argument('--data-dir', default=None, help='数据目录（默认 artifacts/app）')

    restore = commands.add_parser('restore-data', help='从迁移包恢复到新的数据目录',
                                  description='从迁移包恢复到新的数据目录，不覆盖已有数据。')
    restore.add_argument('archive', help='迁移包 ZIP 路径')
    restore.add_argument('--data-dir', required=True, metavar='目录', help='新数据目录，必须尚不存在')

    imports = commands.add_parser('import-knowledge', help='导入整理后的知识包（需先停止服务）',
                                  description='校验并原子替换本地知识库，保留聊天记录与手动知识。')
    imports.add_argument('bundle', help='整理包目录，例如 artifacts/knowledge/knowledge-cleaned')
    imports.add_argument('--data-dir', default='artifacts/app', help='数据目录（默认 artifacts/app）')

    feishu = commands.add_parser('feishu-check', help='校验飞书应用凭据与长连接（不启动接待）',
                                 description='用已保存的应用机器人配置检查飞书连通性，可选用测试消息。')
    feishu.add_argument('--data-dir', default='artifacts/app', help='数据目录（默认 artifacts/app）')
    feishu.add_argument('--connect', action='store_true', help='建立长连接并等待确认（最多 20 秒）')
    feishu.add_argument('--send', action='store_true', help='向已保存的通知会话发送一条不含客户资料的测试消息')

    observe = commands.add_parser('observe-desktop', help='采集客户端截图与 OCR 标注，用于槽位标定',
                                  description='只读采集桌面客户端截图与 OCR 结果；产物含客户端文字，请勿外发。')
    observe.add_argument('--data-dir', default='artifacts/app', help='数据目录（默认 artifacts/app）')
    observe.add_argument('--out', default=None, help='输出目录（默认 数据目录/observations）')
    observe.add_argument('--no-wait', action='store_true', help='不等待手动打开会话')

    return parser


def run_serve(args):
    from cs_duty.server import serve
    return serve(args.host, args.port, args.data_dir)


def run_restore(args):
    import sqlite3

    from cs_duty.data_management import restore_workspace
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
        print('请在管理页重新填写模型密钥、Linkr Token 和飞书通知配置。')
    print('请在客户端重新登录，并确认 Linkr 连接正常。')
    return 0


def run_import_knowledge(args):
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


def run_feishu_check(args):
    import time
    from pathlib import Path

    from cs_duty.database import Database
    from cs_duty.feishu import FeishuBridge, FeishuError, check
    from cs_duty.settings import Settings
    db = Database(Path(args.data_dir) / 'business.sqlite3')
    try:
        settings = Settings(db, None)
        result = check(settings.runtime(), send=args.send)
        print('应用认证成功，机器人 open_id：' + result['bot'])
        if result['sent']:
            print('测试通知已发送到：' + result['chat'])
        if args.connect:
            bridge = FeishuBridge(db, settings)
            try:
                bridge.start()
                deadline = time.time() + 20
                while time.time() < deadline and bridge.status()['state'] not in ('connected', 'error'):
                    time.sleep(.2)
                status = bridge.status()
                if status['state'] != 'connected':
                    raise FeishuError('长连接未建立：' + status['detail'])
                print('飞书长连接已建立，可以接收待办处理结果。')
            finally:
                bridge.stop()
    except FeishuError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


def run_observe(args):
    from pathlib import Path

    from cs_duty.database import Database
    from cs_duty.desktop.observe import capture
    from cs_duty.settings import Settings
    data_dir = Path(args.data_dir)
    db = Database(data_dir / 'business.sqlite3')
    try:
        target = capture(Settings(db, None).runtime(), data_dir, args.out, wait=not args.no_wait)
    finally:
        db.close()
    print('采集完成：' + target)
    print('产物包含客户端文字，请确认不含需要保密的客户信息后再外发。')
    return 0


HANDLERS = {'serve': run_serve, 'restore-data': run_restore, 'import-knowledge': run_import_knowledge,
            'feishu-check': run_feishu_check, 'observe-desktop': run_observe}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    argv = list(sys.argv[1:] if argv is None else argv) or ['serve']
    args = build_parser().parse_args(argv)
    return HANDLERS[args.command](args)


if __name__ == '__main__':
    raise SystemExit(main())
