#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""最小 Agent 客户端示例：把一份 JSON 通过 HTTP 接口送给应用（生成草稿，等待人工审核）。

用法：
    python tools/agent_client.py tools/sample_ingestion.json
    python tools/agent_client.py tools/sample_ingestion.json --base http://127.0.0.1:8765
"""
import argparse
import json
import sys
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="识别结果 JSON 文件")
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    args = ap.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        payload = json.load(f)

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        args.base.rstrip("/") + "/api/ingestions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode("utf-8", "replace"))
        return 1
    print(json.dumps(body, ensure_ascii=False, indent=2))
    if body.get("batch_id"):
        print("\n下一步：打开", args.base.rstrip("/") + body.get("review_url"), "人工确认。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
