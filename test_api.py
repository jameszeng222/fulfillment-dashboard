"""端到端验证系统 API。"""
import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"


class Client:
    def __init__(self):
        self.cookie = ""

    def req(self, path, data=None, method=None):
        h = {"Content-Type": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        body = json.dumps(data).encode() if data is not None else None
        path = urllib.parse.quote(path, safe="/?=&#%:,")
        r = urllib.request.Request(BASE + path, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                ck = resp.headers.get("set-cookie")
                if ck:
                    self.cookie = ck.split(";")[0]
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def login(self, u="admin", p="admin123"):
        return self.req("/api/login", {"username": u, "password": p})


ok = True


def check(cond, label, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {label}" + (f" — {extra}" if extra else ""))


c = Client()
print("== 认证 ==")
code, d = c.req("/api/me")
check(code == 401, "未登录时 /api/me 返回 401", code)
code, d = c.login("admin", "wrong")
check(code == 401, "错误密码被拒", code)
code, d = c.login()
check(code == 200 and d.get("user"), "登录成功", (d.get("user") or {}).get("display_name"))
code, d = c.req("/api/me")
check(code == 200 and d.get("username") == "admin", "会话有效")

print("\n== 元数据 ==")
code, m = c.req("/api/meta")
check(code == 200 and len(m["warehouses"]) > 5, "仓库选项", len(m["warehouses"]))
check(len(m["countries"]) > 50, "国家选项", len(m["countries"]))
check(len(m["rules"]) == 7, "预警规则", [r["rule_cn"] for r in m["rules"]])

print("\n== 订单 ==")
code, d = c.req("/api/orders?size=3")
check(code == 200 and d["total"] > 1_000_000, "总数", f"{d['total']:,}")
check(len(d["rows"]) == 3, "每页条数")
r0 = d["rows"][0]
check("履约单号" in r0 and "物流商" in r0, "字段完整", list(r0)[:4])
code, d2 = c.req("/api/orders?size=3&page=2")
check(d2["rows"][0]["履约单号"] != r0["履约单号"], "翻页取到不同数据")

code, d = c.req("/api/orders?size=3&wh=XC01")
check(all(r["仓库"] == "XC01" for r in d["rows"]), "仓库筛选生效", d["total"])

code, d = c.req("/api/orders?size=3&q=" + r0["履约单号"])
check(d["total"] >= 1, "单号搜索命中", d["total"])

print("\n== 订单详情 ==")
code, det = c.req("/api/orders/" + r0["履约单号"])
check(code == 200 and det["order"]["履约单号"] == r0["履约单号"], "详情可取")
check(isinstance(det["skus"], list) and len(det["skus"]) >= 1, "SKU 明细", len(det["skus"]))
check("alerts" in det and "milestones" in det, "含预警与轨迹字段")
code, _ = c.req("/api/orders/P_NOT_EXIST")
check(code == 404, "不存在的单返回 404", code)

print("\n== 预警 ==")
code, d = c.req("/api/alerts?size=5&state=open")
check(code == 200 and d["total"] > 10000, "预警总数", f"{d['total']:,}")
check(all("state" in r for r in d["rows"]), "带处理状态字段")
a0 = d["rows"][0]
check(a0["state"] == "open", "默认待处理", a0["state"])

code, d5 = c.req("/api/alerts?size=5&level=" + urllib.parse.quote("紧急"))
check(all(r["level"] == "紧急" for r in d5["rows"]), "级别筛选", d5["total"])

print("\n== 预警操作 ==")
code, r = c.req("/api/alerts/action",
                {"order_no": a0["order_no"], "rule": a0["rule"], "action": "claim"})
check(code == 200 and r["status"] == "claimed", "认领成功", r.get("status"))

code, d = c.req("/api/alerts?size=50&state=claimed")
hit = [x for x in d["rows"] if x["order_no"] == a0["order_no"]]
check(len(hit) == 1 and hit[0]["state"] == "claimed", "状态已持久化")

code, r = c.req("/api/alerts/action",
                {"order_no": a0["order_no"], "rule": a0["rule"], "action": "resolve",
                 "note": "已联系承运商"})
check(code == 200, "标记已解决")
code, d = c.req("/api/alerts?size=50&state=resolved")
hit = [x for x in d["rows"] if x["order_no"] == a0["order_no"]]
check(len(hit) == 1, "已解决可见")

print("\n== 订单备注 ==")
code, _ = c.req(f"/api/orders/{a0['order_no']}/notes", {"body": "测试备注：等待客户回复"})
check(code == 200, "写备注")
code, det = c.req("/api/orders/" + a0["order_no"])
check(any("测试备注" in (n["body"] or "") for n in det["notes"]), "备注可读回")

print("\n== 统计 ==")
code, s = c.req("/api/stats")
check(code == 200 and s["orders"] > 1_000_000, "总单量", f"{s['orders']:,}")
check(s["signRate"] and 80 < s["signRate"] < 100, "签收率", round(s["signRate"], 1))
check(s["rate7"] and s["rate7"] > 80, "7天达成率", round(s["rate7"], 1))
check(len(s["months"]) == 11, "月度序列", len(s["months"]))

print("\n== 权限 ==")
c2 = Client()
c2.login("kefu", "kefu123")
code, me2 = c2.req("/api/me")
check(code == 200 and me2["role"] == "cs", "客服账号角色", me2.get("role"))
code, r = c2.req("/api/alerts/action",
                 {"order_no": a0["order_no"], "rule": a0["rule"], "action": "reopen"})
check(code == 200, "客服也可操作")

print("\n== 退出 ==")
code, _ = c.req("/api/logout", {})
code, d = c.req("/api/me")
check(code == 401, "退出后会话失效", code)

print("\n" + ("全部通过 ✅" if ok else "存在问题 ❌"))
raise SystemExit(0 if ok else 1)
