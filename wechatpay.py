"""WeChat Pay v3 Native（PC扫码）— 公钥模式。

微信支付 2024+ 新商户不再提供平台证书下载，改用「微信支付公钥」。
本模块用公钥模式初始化，无需下载平台证书。

凭证环境变量：
  WECHAT_MCHID              商户号
  WECHAT_APPID              关联的 AppID
  WECHAT_APIV3_KEY          APIv3 密钥（32 位）
  WECHAT_CERT_SERIAL        商户证书序列号（40 位）
  WECHAT_PRIVATE_KEY_PATH   apiclient_key.pem 文件路径
  WECHAT_PUBLIC_KEY_SERIAL  微信支付公钥 ID（PUB_KEY_ID_...）
  WECHAT_PUBLIC_KEY_B64     微信支付公钥内容（base64 编码）
"""
import os
import json
import base64

MCHID = os.environ.get("WECHAT_MCHID", "")
APPID = os.environ.get("WECHAT_APPID", "")
APIV3_KEY = os.environ.get("WECHAT_APIV3_KEY", "")
CERT_SERIAL = os.environ.get("WECHAT_CERT_SERIAL", "")
PRIVATE_KEY_PATH = os.environ.get("WECHAT_PRIVATE_KEY_PATH", "")
PUB_KEY_SERIAL = os.environ.get("WECHAT_PUBLIC_KEY_SERIAL", "")
PUB_KEY_B64 = os.environ.get("WECHAT_PUBLIC_KEY_B64", "")

_wxpay = None


def configured() -> bool:
    return bool(MCHID and APPID and APIV3_KEY and CERT_SERIAL and PRIVATE_KEY_PATH)


def _get(notify_url: str = ""):
    global _wxpay
    if _wxpay is None:
        from wechatpayv3 import WeChatPay, WeChatPayType
        kwargs = dict(
            wechatpay_type=WeChatPayType.NATIVE,
            mchid=MCHID,
            private_key=open(PRIVATE_KEY_PATH).read(),
            cert_serial_no=CERT_SERIAL,
            apiv3_key=APIV3_KEY,
            appid=APPID,
            notify_url=notify_url,
            cert_dir="/tmp/wxpay_certs/",
        )
        if PUB_KEY_SERIAL and PUB_KEY_B64:
            kwargs["public_key_id"] = PUB_KEY_SERIAL
            kwargs["public_key"] = base64.b64decode(PUB_KEY_B64).decode()
        _wxpay = WeChatPay(**kwargs)
    return _wxpay


def create_order(order_id: str, amount_cents: int, description: str, notify_url: str) -> str:
    wxpay = _get(notify_url)
    code, message = wxpay.pay(
        description=description[:127],
        out_trade_no=order_id,
        amount={"total": amount_cents, "currency": "CNY"},
    )
    if code >= 300:
        raise RuntimeError(f"WeChat Pay error({code}): {message[:200]}")
    data = json.loads(message)
    return data.get("code_url", "")


def handle_notify(headers: dict, body: bytes) -> dict:
    wxpay = _get()
    return wxpay.callback(headers, body)
