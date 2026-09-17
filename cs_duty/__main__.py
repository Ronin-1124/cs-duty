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
        print('请在客户端重新登录，并确认 Linkr 连接正常。')
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
    if argv and argv[0] == 'feishu-check':
        parser = argparse.ArgumentParser(description='用已保存的应用机器人配置检查飞书连通性（不启动接待）')
        parser.add_argument('--data-dir', default='artifacts/app')
        parser.add_argument('--connect', action='store_true', help='建立长连接并等待确认（最多 20 秒）')
        parser.add_argument('--send', action='store_true', help='向已保存的通知会话发送一条不含客户资料的测试消息')
        args = parser.parse_args(argv[1:])
        import time
        from pathlib import Path
        from cs_duty.database import Database
        from cs_duty.settings import Settings
        from cs_duty.feishu import FeishuBridge, FeishuError, check
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
    if argv and argv[0] == 'observe-desktop':
        parser = argparse.ArgumentParser(description='只读采集桌面客户端截图与 OCR 结果（需先停止服务；含客户端文字，请勿外发）')
        parser.add_argument('--data-dir', default='artifacts/app')
        parser.add_argument('--out', default=None)
        parser.add_argument('--no-wait', action='store_true', help='不等待手动打开会话')
        args = parser.parse_args(argv[1:])
        from pathlib import Path
        from cs_duty.database import Database
        from cs_duty.settings import Settings
        from cs_duty.desktop.observe import capture as capture_desktop
        data_dir = Path(args.data_dir)
        db = Database(data_dir / 'business.sqlite3')
        try:
            target = capture_desktop(Settings(db, None).runtime(), data_dir, args.out, wait=not args.no_wait)
        finally:
            db.close()
        print('采集完成：' + target)
        print('产物包含客户端文字，请确认不含需要保密的客户信息后再外发。')
        return 0
    parser = argparse.ArgumentParser(description='本地客服应用：管理页面、LangGraph 流程与桌面客户端接入')
    parser.add_argument('command', nargs='?', default='serve', choices=['serve'])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18766)
    parser.add_argument('--data-dir', default=None)
    args = parser.parse_args(argv)
    from cs_duty.server import serve
    return serve(args.host, args.port, args.data_dir)


if __name__ == '__main__':
    raise SystemExit(main())
