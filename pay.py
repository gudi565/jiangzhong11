"""支付宝当面付（precreate）—— 个体户官方支付。

流程：alipay.trade.precreate 生成扫码支付码 → 买家支付宝扫码付款
     → 支付宝 POST notify_url → RSA2 验签 → 开通时长。

凭证环境变量（支付宝开放平台拿）：
  ALIPAY_APP_ID          应用 APPID
  ALIPAY_APP_PRIVATE_KEY 应用私钥（RSA2，PEM 或裸 base64）
  ALIPAY_PUBLIC_KEY      支付宝公钥
未配置时走测试模式（直接开通，不真实付款）。
"""
import os
import io
import base64

PLANS = {
    "1h": {"seconds": 3600, "price": "9.90", "name": "1 小时"},
    "1d": {"seconds": 86400, "price": "29.90", "name": "1 天"},
    "7d": {"seconds": 604800, "price": "69.90", "name": "7 天"},
}

APP_ID = os.environ.get("ALIPAY_APP_ID", "")
APP_PRIVATE_KEY = os.environ.get("ALIPAY_APP_PRIVATE_KEY", "")
ALIPAY_PUBLIC_KEY = os.environ.get("ALIPAY_PUBLIC_KEY", "")


def configured() -> bool:
    return bool(APP_ID and APP_PRIVATE_KEY and ALIPAY_PUBLIC_KEY)


_alipay = None


def _get_alipay():
    """懒加载 AliPay 实例（SDK 没装也能 import pay.py 走测试模式）。"""
    global _alipay
    if _alipay is None:
        from alipay import AliPay
        _alipay = AliPay(
            appid=APP_ID,
            app_notify_url=None,
            app_private_key_string=APP_PRIVATE_KEY,
            alipay_public_key_string=ALIPAY_PUBLIC_KEY,
            sign_type="RSA2",
            debug=False,
        )
    return _alipay


def create_order(order_id: str, amount: str, title: str, notify_url: str, return_url: str = "") -> dict:
    """支付宝当面付下单，返回 {'qr_code': 'https://qr.alipay.com/...'}。"""
    alipay = _get_alipay()
    result = alipay.api_alipay_trade_precreate(
        out_trade_no=order_id,
        total_amount=amount,
        subject=title,
        notify_url=notify_url,
    )
    if result.get("code") != "10000":
        raise RuntimeError(f"支付宝下单失败: {result.get('sub_msg') or result.get('msg', '未知错误')}")
    return {"qr_code": result["qr_code"]}


def verify_notify(params: dict) -> bool:
    """支付宝回调验签（RSA2）。"""
    try:
        alipay = _get_alipay()
        data = dict(params)
        sign = data.pop("sign", "")
        data.pop("sign_type", None)
        return alipay.verify(data, sign)
    except Exception:
        return False


def gen_qr_base64(text: str) -> str:
    """把文本（支付链接）生成 QR PNG，返回 data: URI（前端直接 <img src> 显示）。"""
    import qrcode
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
