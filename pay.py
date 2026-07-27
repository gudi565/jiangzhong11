"""虎皮椒（xunhupay）免签支付对接。

流程：下单 -> 虎皮椒返回支付页 URL -> 买家扫码付（个人支付宝/微信）
     -> 虎皮椒监听到账 -> POST notify_url 回调 -> 验签 -> 开通时长。

凭证从环境变量读：XUNHU_APPID / XUNHU_APPSECRET（虎皮椒后台拿）。
未配置时 server 走"测试模式"（直接开通，不真实付款），便于本地演示。
"""
import os
import time
import json
import hashlib
import secrets
import urllib.request

APPID = os.environ.get("XUNHU_APPID", "")
APPSECRET = os.environ.get("XUNHU_APPSECRET", "")
API_URL = "https://api.xunhupay.com/payment/do.html"

# 套餐：plan key -> {seconds, price(元), name}
PLANS = {
    "1h": {"seconds": 3600, "price": "9.90", "name": "1 小时"},
    "1d": {"seconds": 86400, "price": "29.90", "name": "1 天"},
    "7d": {"seconds": 604800, "price": "69.90", "name": "7 天"},
}


def configured() -> bool:
    return bool(APPID and APPSECRET)


def _sign(params: dict) -> str:
    """虎皮椒签名：非空参数（除 hash）按 key ASCII 排序，拼 k=v&…，末尾直接追加 APPSECRET，md5 小写。"""
    items = sorted((k, v) for k, v in params.items() if k != "hash" and str(v) != "")
    s = "&".join(f"{k}={v}" for k, v in items)
    return hashlib.md5((s + APPSECRET).encode("utf-8")).hexdigest()


def create_order(order_id: str, amount: str, title: str, notify_url: str, return_url: str = "") -> dict:
    params = {
        "version": "1.1",
        "appid": APPID,
        "trade_order_id": order_id,
        "total_fee": amount,
        "title": title,
        "time": str(int(time.time())),
        "notify_url": notify_url,
        "nonce_str": secrets.token_hex(8),
    }
    if return_url:
        params["return_url"] = return_url
    params["hash"] = _sign(params)
    body = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def verify_notify(params: dict) -> bool:
    """回调验签：用同样算法重算 hash 比对。"""
    if "hash" not in params:
        return False
    return _sign(params) == params["hash"]
