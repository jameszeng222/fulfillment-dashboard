"""验证四个页面的接口。"""
import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"


class C:
    def __init__(self):
        self.ck = ""

    def req(self, path, data=None):
        h = {"Content-Type": "application/json"}
        if self.ck:
            h["Cookie"] = self.ck
        body = json.dumps(data).encode() if data is not None else None
        path = urllib.parse.quote(path, safe="/?=&#%:,")
        r = urllib.request.Request(BASE + path, data=body, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                ck = resp.headers.get("set-cookie")
                if ck:
                    self.ck = ck.split(";")[0]
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")


ok = True


def ck(cond, label, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {label}" + (f" — {extra}" if extra else ""))


c = C()
s, d = c.req("/api/login", {"username": "admin", "password": "admin123"})
ck(s == 200, "登录", s)

print("\n== 数据总览 ==")
s, d = c.req("/api/overview")
ck(s == 200, "接口 200", s)
k = d["kpi"]
ck(k["orders"] == 1337690, "单量", k["orders"])
ck(len(d["monthly"]) == 11, "月度序列", len(d["monthly"]))
ck(len(d["byCarrier"]) >= 10, "物流渠道占比", len(d["byCarrier"]))
ck(len(d["byWarehouse"]) >= 10, "发货仓分布", len(d["byWarehouse"]))
ck(len(d["byCountry"]) >= 10, "目的国家分布", len(d["byCountry"]))
ck(k["rate6"] and 80 < k["rate6"] < 100, "6天达成率", k["rate6"])
ck(d["byCarrier"][0]["k"] == "WYT-USPS", "第一大渠道", d["byCarrier"][0]["k"])

s, d2 = c.req("/api/overview?wh=" + urllib.parse.quote("XC01"))
ck(d2["kpi"]["orders"] < k["orders"], "仓库筛选生效", d2["kpi"]["orders"])

s, d3 = c.req("/api/overview?month=2026-10")
ck(len(d3["monthly"]) == 1 and d3["monthly"][0]["m"] == "2026-10", "月份筛选生效", d3["monthly"][0]["n"])

print("\n== 轨迹监控 ==")
s, d = c.req("/api/track?size=5")
ck(s == 200, "接口 200", s)
ck(d["total"] > 100000, "总数", f"{d['total']:,}")
ck(len(d["rows"]) == 5, "每页条数")
ck(len(d["statusSummary"]) >= 2, "官方状态汇总", [x["状态"] for x in d["statusSummary"]][:4])
ck(len(d["ruleSummary"]) >= 2, "自定义异常汇总", [x["异常"] for x in d["ruleSummary"]][:4])

s, d = c.req("/api/track?tstat=Exception&size=3")
ck(all(r["轨迹状态"] == "Exception" for r in d["rows"]), "17track 状态筛选", d["total"])

s, d = c.req("/api/track?rule=returning&size=3")
ck(all(r["主预警名称"] == "包裹退运" for r in d["rows"]), "自定义异常筛选", d["total"])

print("\n== 渠道时效 ==")
s, d = c.req("/api/channels")
ck(s == 200, "接口 200", s)
ck(len(d["rows"]) >= 5, "渠道数", len(d["rows"]))
r0 = d["rows"][0]
ck(r0["rate6"] is not None and r0["target"], "含 6天达成率与 SLA 目标", f"{r0['channel']} {r0['rate6']}% / {r0['target']}天")
ck(r0["verdict"] in ("达标", "未达标"), "有达标结论", r0["verdict"])
ck(len(d["byCountry"]) > 0, "渠道×国家", len(d["byCountry"]))
ck(any(r["overdue"] is not None for r in d["rows"]), "含在途超时", "ok")

print("\n== 数据与规则 ==")
s, d = c.req("/api/rules")
ck(s == 200, "接口 200", s)
ck(len(d["rules"]) == 9, "预警规则数", len(d["rules"]))
names = [r["name"] for r in d["rules"]]
ck("物流未上网" in names and "运输超时" in names and "海关卡关" in names, "三条关键规则在列", names[:3])
ck(all(r.get("trigger") and r.get("exclude") for r in d["rules"]), "规则含判定口径")
ck(len(d["notImplemented"]) == 3, "未接入规则", [x["name"] for x in d["notImplemented"]])
ck(len(d["channels"]) >= 5, "SLA 渠道表", len(d["channels"]))
ck(any(r["target"] != 6 for r in d["channels"]), "存在非默认 SLA", "postnet 12 天")

print("\n" + ("全部通过 ✅" if ok else "存在问题 ❌"))
raise SystemExit(0 if ok else 1)
