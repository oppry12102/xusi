#!/usr/bin/env python3
"""全队普查：内核版本 / BOOT.md 大小 / xmem 启用态。一次性调查脚本（用后即弃）。"""
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from xusi.remote import run_remote  # noqa: E402

ROOT = Path(__file__).parent
GATHER = r"""
cd ~/work/xusi 2>/dev/null || exit 1
for d in instances/*/; do
  id=${d%/}; id=${id##*/}
  ver=$(grep -m1 '^version' "$d/xuseek-v2/pyproject.toml" 2>/dev/null | grep -o '"[0-9][^"]*"' | tr -d '"')
  [ -z "$ver" ] && ver=$(grep -m1 '_FALLBACK_VERSION' "$d/xuseek-v2/xuseek/__init__.py" 2>/dev/null | grep -o '"[0-9][^"]*"' | tr -d '"')
  boot=$(stat -c%s "$d/workspace/BOOT.md" 2>/dev/null || echo MISSING)
  xm=$(grep -A4 '^\[xmem\]' "$d/config.toml" 2>/dev/null | grep -m1 'enabled' | tr -d ' ')
  xdb=$(stat -c%s "$d/data/xmem.db" 2>/dev/null || echo NONE)
  echo "$id|${ver:-?}|$boot|${xm:-no-section}|$xdb"
done
"""


def survey_local() -> list[tuple[str, str, str, str, str]]:
    rows = []
    for d in sorted((ROOT / "instances").glob("agent-*/")):
        if not d.is_dir():
            continue
        aid = d.name
        ver = "?"
        for p in (d / "xuseek-v2" / "pyproject.toml",):
            try:
                m = re.search(r'^version\s*=\s*"([^"]+)"', p.read_text(), re.M)
                ver = m.group(1)
            except Exception:
                pass
        boot = "MISSING"
        b = d / "workspace" / "BOOT.md"
        if b.exists():
            boot = str(b.stat().st_size)
        xm = "no-section"
        try:
            cfg = tomllib.loads((d / "config.toml").read_text())
            if "xmem" in cfg:
                xm = f"enabled={bool(cfg['xmem'].get('enabled'))}"
        except Exception:
            pass
        xdb = "NONE"
        x = d / "data" / "xmem.db"
        if x.exists():
            xdb = str(x.stat().st_size)
        rows.append((aid, ver, boot, xm, xdb))
    return rows


def main() -> None:
    hosts = tomllib.loads((ROOT / "etc" / "hosts.toml").read_text())["host"]
    print("host|agent|kernel|boot_bytes|xmem|db_bytes")
    for row in survey_local():
        print("LOCAL|" + "|".join(row))
    for h in hosts:
        name = h["name"]
        try:
            cp = run_remote(h, GATHER, timeout=180)
            if cp.returncode != 0:
                print(f"{name}|ERROR|rc={cp.returncode}|{(cp.stderr or '')[:80]}")
                continue
            for line in cp.stdout.strip().splitlines():
                print(f"{name}|{line}")
        except Exception as e:
            print(f"{name}|ERROR|{type(e).__name__}: {str(e)[:100]}")


if __name__ == "__main__":
    main()
