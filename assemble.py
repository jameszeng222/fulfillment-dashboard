"""
组装看板单文件。

输入:
  dashboard_src/app.template.html   页面模板(含 <!--ECHARTS--> 与 __DATA__ 占位)
  dashboard_src/cube.json           立方体数据(build_cube.py 产出)
  dashboard_src/echarts.min.js      ECharts 库(已 vendor 进仓库)
输出:
  public/index.html                 部署入口(Cloudflare Pages 的静态根目录)
  履约单看板.html                    本地双击用的同名副本

用法:
  python build_cube.py && python assemble.py
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TPL = ROOT / "dashboard_src" / "app.template.html"
CUBE = ROOT / "dashboard_src" / "cube.json"
LIB = ROOT / "dashboard_src" / "echarts.min.js"
OUT_PUBLIC = ROOT / "public" / "index.html"
OUT_LOCAL = ROOT / "履约单看板.html"


def main():
    if not CUBE.exists():
        raise SystemExit("缺少 dashboard_src/cube.json, 先跑 python build_cube.py")
    for p, desc in [(TPL, "页面模板"), (LIB, "ECharts 库")]:
        if not p.exists():
            raise SystemExit(f"缺少{desc}: {p}")

    tpl = TPL.read_text(encoding="utf-8")
    cube = CUBE.read_text(encoding="utf-8")
    lib = LIB.read_text(encoding="utf-8")

    if tpl.count("<!--ECHARTS-->") != 1:
        raise SystemExit(
            f"模板里 <!--ECHARTS--> 必须恰好出现 1 次, 实际 {tpl.count('<!--ECHARTS-->')} 次")
    if tpl.count("__DATA__") != 1:
        raise SystemExit(
            f"模板里 __DATA__ 必须恰好出现 1 次, 实际 {tpl.count('__DATA__')} 次 —— "
            "提示文案里不要写这个字面量, 否则数据会被注入多次导致文件翻倍")

    html = tpl.replace("<!--ECHARTS-->", lib).replace("__DATA__", cube)

    OUT_PUBLIC.parent.mkdir(exist_ok=True)
    OUT_PUBLIC.write_text(html, encoding="utf-8")
    shutil.copy2(OUT_PUBLIC, OUT_LOCAL)

    kb = len(html.encode("utf-8")) / 1024
    print(f"[ok] ECharts {len(lib)/1024:.0f} KB + 立方体 {len(cube)/1024:.0f} KB")
    print(f"[ok] 写出 public/index.html 与 {OUT_LOCAL.name}  {kb:.0f} KB")


if __name__ == "__main__":
    main()
