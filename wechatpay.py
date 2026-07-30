"""WeChat Pay v3 Native（PC 扫码支付）官方对接。

凭证从环境变量读：
  WECHAT_MCHID           商户号
  WECHAT_APPID           关联的公众号/小程序 AppID
  WECHAT_APIV3_KEY       APIv3 密钥（32 位）
  WECHAT_CERT_SERIAL     证书序列号（40 位）
  WECHAT_PRIVATE_KEY_PATH  apiclient_key.pem 文件路径
"""
import os
import json

MCHID = os.environ.get("WECHAT_MCHID", "")
APPID = os.environ.get("WECHAT_APPID", "")
APIV3_KEY = os.environ.get("WECHAT_APIV3_KEY", "")
CERT_SERIAL = os.environ.get("WECHAT_CERT_SERIAL", "")
PRIVATE_KEY_PATH = os.environ.get("WECHAT_PRIVATE_KEY_PATH", "/opt/jiangzhong/cert/apiclient_key.pem")

_wxpay = None


def configured() -> bool:
    return bool(MCHID and APPID and APIV3_KEY and CERT_SERIAL)


def _get(notify_url: str = ""):
    global _wxpay
    if _wxpay is None:
        from wechatpayv3 import WeChatPay, WeChatPayType
        _wxpay = WeChatPay(
            wechatpay_type=WeChatPayType.NATIVE,
            mchid=MCHID,
            private_key=PRIVATE_KEY_PATH,
            cert_serial_no=CERT_SERIAL,
            apiv3_key=APIV3_KEY,
            appid=APPID,
            notify_url=notify_url,
            cert_dir="/tmp/wxpay_certs/",
        )
    return _wxpay


def create_order(order_id: str, amount_cents: int, description: str, notify_url: str) -> str:
    """创建 Native 扫码订单，返回 code_url（weixin://wxpay/...）。"""
    wxpay = _get(notify_url)
    code, message = wxpay.pay(
        description=description[:127],
        out_trade_no=order_id,
        amount={"total": amount_cents, "currency": "CNY"},
    )
    if code >= 300:
        raise RuntimeError(f"WeChat Pay 下单失败({code}): {message[:200]}")
    data = json.loads(message)
    return data.get("code_url", "")


def handle_notify(headers: dict, body: bytes) -> dict:
    """验证 + 解密微信支付回调通知。成功返回解密后的 dict，失败返回 None。"""
    wxpay = _get()
    return wxpay.callback(headers, body)
