# -*- coding: utf-8 -*-
"""生成 assets/sw2_whitelist.json —— 板块轮动用的「申万二级」板块白名单。

为什么要固化成文件而不是运行时调 akshare：
  1. 渲染报告时不该依赖联网；白名单是口径资产，不是行情数据
  2. 口径必须稳定可复现 —— 申万哪天调整名单，报告历史就会变，那是 bug

为什么是 128 条而不是申万官方的 131 条：
  申万 2021 把「银行」拆成 4 个二级（国有大型银行Ⅱ / 股份制银行Ⅱ / 城商行Ⅱ /
  农商行Ⅱ），东财只给一个「银行Ⅱ」，层级差一层。照搬 131 条时，10-27 的
  Top2 会算成「股份制银行Ⅲ +1.61」「城商行Ⅲ +1.55」，而东财 App 是
  「银行Ⅱ +1.34」排第 2 —— 差两格。
  剔除 4 个细分、补回「银行Ⅱ」后，与东财 App 榜单逐格一致（10-26/27/28 共
  24 格全对）。

跑法：
    python scripts/make_sw2_whitelist.py            # 重新生成（需联网，要 akshare）
    python scripts/make_sw2_whitelist.py --check    # 只校验现有文件，不联网
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import WHITELIST_PATH  # noqa: E402

# 东财没有这个层级的拆分，剔除后补一条合并项
SW2_BANK_SPLIT = ["国有大型银行Ⅱ", "股份制银行Ⅱ", "城商行Ⅱ", "农商行Ⅱ"]
BANK_MERGED = "银行Ⅱ"


def build():
    import akshare as ak  # 延迟导入：--check 不需要它

    df = ak.sw_index_second_info()
    names = [str(x) for x in df["行业名称"].astype(str).tolist()]
    dropped = [n for n in names if n in SW2_BANK_SPLIT]
    kept = [n for n in names if n not in SW2_BANK_SPLIT]
    if BANK_MERGED not in kept:
        kept.append(BANK_MERGED)
    return {
        "meta": {
            "source": "akshare sw_index_second_info（申万 2021 版二级）",
            "adjust": ("申万把银行拆成 4 个二级，东财只有一个「银行Ⅱ」；"
                       "剔除 4 个细分、补回「银行Ⅱ」，与东财 App 榜单对齐"),
            "dropped": dropped,
            "added": [BANK_MERGED],
            "sw2_raw_count": len(names),
            "count": len(kept),
            "calibrated_on": "2026-10-26 / 10-27 / 10-28 三日 Top8 共 24 格全部一致",
        },
        "names": sorted(kept),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验现有文件，不联网")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    if a.check:
        if not os.path.exists(WHITELIST_PATH):
            print(f"[缺失] {WHITELIST_PATH}")
            raise SystemExit(1)
        d = json.load(open(WHITELIST_PATH, encoding="utf-8"))
        names = d.get("names") or []
        print(f"白名单文件: {WHITELIST_PATH}")
        print(f"条数      : {len(names)}")
        print(f"银行细分已剔除: {[n for n in SW2_BANK_SPLIT if n in names] or '是'}")
        print(f"含 {BANK_MERGED}: {'是' if BANK_MERGED in names else '否 ← 有问题'}")
        raise SystemExit(0 if (BANK_MERGED in names and len(names) >= 120) else 1)

    data = build()
    os.makedirs(os.path.dirname(WHITELIST_PATH), exist_ok=True)
    with open(WHITELIST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"已写入 {WHITELIST_PATH}")
    print(f"申万二级原始 {data['meta']['sw2_raw_count']} 条 → 白名单 {data['meta']['count']} 条")
    print(f"剔除: {data['meta']['dropped']}")
    print(f"补回: {data['meta']['added']}")


if __name__ == "__main__":
    main()
