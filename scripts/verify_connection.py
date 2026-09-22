"""真机验证 PR-2 连接模型（零配置发现）。无引擎依赖，除非对活跃工程做端到端 ping。

用法：
    python scripts/verify_connection.py            # 用真实 registry 目录（默认用户级）
    python scripts/verify_connection.py <REG_DIR>  # 指定 registry 目录（做隔离测试）
    python scripts/verify_connection.py --ping      # 额外对唯一活跃工程发一次真 ping

它按 §3.3 判定阶梯逐步打印：registry 原始指针 -> discover 新鲜度 -> _resolve 自动定位 ->
（可选）端到端 ping。用来肉眼确认"开编辑器 -> 自注册 -> 零配置发现"闭环成立。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

args = [a for a in sys.argv[1:]]
do_ping = "--ping" in args
args = [a for a in args if a != "--ping"]
if args:
    os.environ["PRISM_REGISTRY_DIR"] = args[0]

# 模拟"零配置 server"：清掉显式工程/总线配置与 _CFG，只留 registry 发现
os.environ.pop("PRISM_PROJECT_DIR", None)
os.environ.pop("PRISM_BUS_DIR", None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows GBK 控制台兼容
except Exception:
    pass

from prism import registry, server, bus  # noqa: E402

server._CFG["bus_dir"] = None
server._CFG["project_dir"] = None


def main():
    rdir = registry.registry_dir()
    print("== registry 目录 ==")
    print("  ", rdir, "(存在)" if os.path.isdir(rdir) else "(不存在 — 还没有任何工程自注册过)")

    entries = registry._load_all()
    print("\n== 原始指针文件 (%d) ==" % len(entries))
    for e in entries:
        print("  - slug=%s bus=%s ue=%s pid=%s" % (e.get("slug"), e.get("bus_dir"), e.get("ue_version"), e.get("pid")))

    disc = registry.discover()
    print("\n== discover（含现场心跳新鲜度）==")
    if not disc:
        print("  (空)")
    for e in disc:
        print("  - %-22s live=%-5s age=%s" % (
            e.get("slug"), e.get("live"),
            ("%.1fs" % e["heartbeat_age"]) if e.get("heartbeat_age") is not None else "无心跳文件"))

    live = [e for e in disc if e["live"]]
    print("\n== _resolve() 自动定位（不传 project=）==")
    _b, _p, err = server._resolve()
    if err:
        print("  ->", err["error"]["code"], ":", err["error"]["message"])
    else:
        print("  -> 自动选中 project_dir =", _p)
        print("     bus_dir =", _b)

    print("\n== 判定阶梯回归 ==")
    print("  活跃工程数 =", len(live), "| 显式配置 =", server._has_explicit(), "(应为 False)")

    if do_ping and len(live) == 1:
        print("\n== 端到端 ping（对唯一活跃工程）==")
        env = server.ping()
        if env.get("ok"):
            r = env["result"]
            print("  ok: bridge_alive=%s ue_version=%s loaded_maps=%s" % (
                r.get("bridge_alive"), r.get("ue_version"), r.get("loaded_maps")))
        else:
            print("  ERR:", env["error"]["code"], env["error"]["message"])
    elif do_ping:
        print("\n== 端到端 ping 跳过：活跃工程数!=1 ==")

    print("\n结论：", "[OK] 发现链路可用" if entries else "[..] 还没捕获到自注册——先开编辑器起 bridge(见 docs/SETUP.md §2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())