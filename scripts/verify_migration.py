"""真机验证 PR-3/PR-4：迁移预览 + 受控执行。需编辑器在线且 bridge 自注册（走零配置发现）。

用法：
    python scripts/verify_migration.py /Game/Path/Asset NewName            # 仅预览（不写）
    python scripts/verify_migration.py /Game/Path/Asset NewName --apply    # 预览后双钥真改名
    python scripts/verify_migration.py /Game/Path/Asset --dest /Game/NewDir            # 预览移动
    python scripts/verify_migration.py /Game/Path/Asset --dest /Game/NewDir --apply    # 真移动
建议先在一个可回滚的靶子资产上试；--apply 后按提示用 get_asset_references 复核，或再跑一次反向改名回滚。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 模拟零配置 server：清显式配置，仅靠 registry 发现活跃工程
os.environ.pop("PRISM_PROJECT_DIR", None)
os.environ.pop("PRISM_BUS_DIR", None)
from prism import server, registry  # noqa: E402

server._CFG["bus_dir"] = None
server._CFG["project_dir"] = None


def _show(env):
    import json
    print(json.dumps(env, ensure_ascii=False, indent=2))
    return env.get("ok")


def main():
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        return 2
    apply_it = "--apply" in args
    args = [a for a in args if a != "--apply"]
    dest = None
    if "--dest" in args:
        i = args.index("--dest")
        dest = args[i + 1]
        del args[i:i + 2]
    asset_path = args[0]
    new_name = args[1] if len(args) > 1 and not args[1].startswith("--") else None

    live = registry.live_entries()
    print("== 活跃工程 ==")
    for e in live:
        print("   %s  bus=%s" % (e["slug"], e["bus_dir"]))
    if len(live) != 1:
        print("[..] 期望恰好一个活跃工程（否则给工具传 project=）。当前 %d 个。" % len(live))
        return 2

    if dest:
        prev = server.preview_asset_migration(asset_path, new_name=new_name, dest_path=dest)
    else:
        if not new_name:
            print("[..] 改名需给 NewName")
            return 2
        prev = server.preview_asset_migration(asset_path, new_name=new_name)
    print("\n== 预览 (PR-3 preview_asset_migration) ==")
    if not _show(prev):
        return 1
    r = prev["result"]
    if r["conflict"]:
        print("\n[拒绝] 目标已存在，un-apply。")
        return 1
    if not apply_it:
        print("\n[dry-run] 未写。加 --apply 且满足双钥才会真正执行。")
        return 0

    print("\n== 执行 (双钥 dry_run=False + confirm=True) ==")
    if dest:
        env = server.migrate_asset_move(asset_path, dest_path=dest, new_name=new_name, dry_run=False, confirm=True)
        restore = "python scripts\\verify_migration.py %s --dest %s --apply   # 反移回" % (r["target"], r["source"]["path"].rsplit("/", 1)[0])
    else:
        env = server.migrate_asset_rename(asset_path, new_name, dry_run=False, confirm=True)
        old_leaf = asset_path.rstrip("/").rsplit("/", 1)[-1]
        restore = "python scripts\\verify_migration.py %s %s --apply   # 改回原名" % (r["target"], old_leaf)
    _show(env)
    print("\n下一步：在编辑器里确认引用已修复（必要时保存资产/提交 VCS）。回滚命令：")
    print("   " + restore)
    return 0


if __name__ == "__main__":
    sys.exit(main())