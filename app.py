import os
import time
from collections import defaultdict, deque
from typing import Optional

from flask import Flask, request
import requests

# --- OpenAI 新SDK ---
from openai import OpenAI, RateLimitError
import base64, json

# ========= 環境 =========
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = Flask(__name__)
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
AI_DISABLED = os.getenv("DISABLE_AI") == "1"   # ← 環境変数でAIを一時停止できる（"1"で有人モード）

# ========= スパム/嫌がらせ対策（閾値は運用に合わせて調整） =========
RATE_MIN_INTERVAL_SEC = 5        # 1ユーザーの最小インターバル（秒）…5秒以内の連投を無視
IMG_MAX_BYTES = 2 * 1024 * 1024  # 画像サイズ上限（2MB）
IMG_MAX_PER_MIN = 3              # 1分あたり最大画像枚数
MSG_MAX_PER_MIN = 15             # 1分あたり最大メッセージ数
TEMP_BLOCK_MINUTES = 30          # 一時ブロック時間（分）

# 状態（インメモリ）。Render再起動でリセットされる想定。
_last_msg_time = defaultdict(float)                  # userId -> 最終受信時刻
_img_history = defaultdict(lambda: deque(maxlen=60)) # userId -> 直近60秒の画像受信タイムスタンプ
_msg_history = defaultdict(lambda: deque(maxlen=60)) # userId -> 直近60秒の全メッセージ受信TS
_blocked_until = defaultdict(float)                  # userId -> ブロック解除UNIX時刻


# ---------------------------
# 返信ユーティリティ
# ---------------------------
def reply_text(reply_token: str, text: str):
    """通常のテキスト返信"""
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": text[:5000]}],
            },
            timeout=10,
        )
    except Exception:
        app.logger.exception("LINE reply failed")


def reply_text_with_quick(reply_token: str, text: str, quick_items=None):
    """クイックリプライ付き返信"""
    body = {
        "replyToken": reply_token,
        "messages": [{
            "type": "text",
            "text": text[:5000],
            **({"quickReply": {"items": quick_items}} if quick_items else {})
        }]
    }
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json=body, timeout=10
        )
    except Exception:
        app.logger.exception("LINE quick reply failed")


# ---------------------------
# 画像取得
# ---------------------------
def get_line_image_bytes(message_id: str) -> bytes:
    """LINEの画像コンテンツをバイト列で取得"""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    r = requests.get(url, headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}, timeout=20)
    r.raise_for_status()
    return r.content


# ---------------------------
# 査定ロジック（AI）
# ---------------------------
ASSESS_SYSTEM = (
    "あなたはリユースショップの査定担当AIです。出力は必ずJSONのみ。"
    "ユーザーに見せるのは『買取目安金額』だけ。中古相場や新品価格の金額は表示しない。"
    "内部ロジックとして、中古相場×0.1~0.2 または 新品価格×0.05~0.1 を目安に計算してよいが、"
    "その根拠や相場の金額は一切表示しない。"
    "項目: category, brand, model, estimate_low, estimate_high, popularity_hint, tips。"
    "estimate_* はJPYの整数。popularity_hint は true/false（SOLDOUTが多い等で人気・上振れ余地があればtrue）。"
    "tips は確認ポイント（傷/付属品/動作など）を簡潔に。"
)

def assess_from_text_or_image(user_text: str = "", image_bytes: Optional[bytes] = None) -> dict:
    """
    テキスト/画像から査定（買取目安のみ返す）
    429（残高不足）は {"error":"quota"} を返す
    """
    if AI_DISABLED:
        return {"error": "disabled"}

    content = []
    if user_text:
        content.append({"type": "text", "text": f"対象情報:\n{user_text}"})
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    try:
        # コスト抑制：画像もテキストも gpt-4o-mini に統一
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": ASSESS_SYSTEM},
                {"role": "user", "content": content},
            ],
            temperature=0.2,
            timeout=25,
        )
        txt = resp.choices[0].message.content
        return json.loads(txt)
    except RateLimitError:
        return {"error": "quota"}
    except Exception as e:
        return {"error": "parse_failed", "raw": str(e)}


# ---------------------------
# スパム/嫌がらせ対策ヘルパ
# ---------------------------
def is_blocked(user_id: str) -> bool:
    return time.time() < _blocked_until[user_id]

def block_user(user_id: str, minutes: int = TEMP_BLOCK_MINUTES):
    _blocked_until[user_id] = time.time() + minutes * 60

def too_frequent(user_id: str) -> bool:
    """最小インターバル未満の連投をブロック"""
    now = time.time()
    if now - _last_msg_time[user_id] < RATE_MIN_INTERVAL_SEC:
        return True
    _last_msg_time[user_id] = now
    return False

def record_and_check_limits(user_id: str, is_image: bool) -> Optional[str]:
    """
    1分あたりの画像/メッセージ数を記録し、超過したら警告文を返す。
    超過が酷い場合は一時ブロック。
    """
    now = time.time()
    # 直近60秒の履歴に現在時刻をpush
    _msg_history[user_id].append(now)
    # 60秒前より古いものを除去
    while _msg_history[user_id] and now - _msg_history[user_id][0] > 60:
        _msg_history[user_id].popleft()

    if len(_msg_history[user_id]) > MSG_MAX_PER_MIN:
        block_user(user_id)  # 全体メッセージ過多
        return "短時間に多数のメッセージを受信したため、一時的に受付を停止しました。しばらく経ってからお試しください。"

    if is_image:
        _img_history[user_id].append(now)
        while _img_history[user_id] and now - _img_history[user_id][0] > 60:
            _img_history[user_id].popleft()
        if len(_img_history[user_id]) > IMG_MAX_PER_MIN:
            block_user(user_id)  # 画像連投過多
            return "画像の連続送信が多いため、一時的に受付を停止しました。1分ほど時間を空けてお試しください。"

    return None


# ---------------------------
# ヘルスチェック
# ---------------------------
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


# ---------------------------
# Webhook
# ---------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        body = request.get_json(force=True, silent=True) or {}
        events = body.get("events", [])

        for ev in events:
            if ev.get("type") != "message":
                continue

            source = ev.get("source", {}) or {}
            user_id = source.get("userId") or "unknown"
            if is_blocked(user_id):
                # ブロック中は黙って無視（200で返す）
                continue
            if too_frequent(user_id):
                # 連投は無視
                continue

            msg = ev.get("message", {})
            reply_token = ev.get("replyToken")
            mtype = msg.get("type")

            # メッセージ制限の記録（画像/テキスト共通）
            over_msg = record_and_check_limits(user_id, is_image=(mtype == "image"))
            if over_msg:
                reply_text(reply_token, over_msg)
                continue

            # ---------- テキスト ----------
            if mtype == "text":
                user_text = msg.get("text", "")
                text = user_text.strip()

                # 1) リッチメニューの固定文言に対応
                if text == "AI査定":
                    reply_text(reply_token, "📸 AI査定を開始します。\n商品の写真（正面や型番ラベル）や型番テキストを送ってください。")
                    continue
                elif text == "お問い合わせ":
                    reply_text(reply_token, "📩 お問い合わせありがとうございます。\n内容をこちらに送信してください。スタッフが手動で返信いたします。")
                    continue
                elif text == "出張買取を依頼":
                    reply_text(reply_token, "🚛 出張買取の仮予約を開始します。\nご希望の訪問日時をお知らせください。")
                    continue

                # 2) それ以外のテキストは → 査定（買取目安のみ）
                data = assess_from_text_or_image(user_text=text)

                # フォールバック（AI停止 or 残高不足）
                if data.get("error") in ("disabled", "quota"):
                    reply_text(
                        reply_token,
                        "現在、AI査定がご利用いただけません。\n"
                        "・写真と型番をこのまま送ってください（スタッフが手動で査定）\n"
                        "・または「出張買取を依頼」を選んで仮予約できます。"
                    )
                    continue

                if data.get("error"):
                    app.logger.warning(f"assess error: {data}")
                    reply_text(reply_token, "うまく解析できませんでした。写真や型番ラベルの画像も送ってください。")
                    continue

                low, high = data.get("estimate_low"), data.get("estimate_high")
                cat, brand, model = data.get("category", ""), data.get("brand", ""), data.get("model", "")
                tips = data.get("tips", "")
                pop = data.get("popularity_hint", False)

                lines = [
                    "🧮 仮査定の買取目安です。",
                    f"・商品：{cat} / {brand} {model}".strip(" /"),
                    f"・買取目安：{int(low):,}円 〜 {int(high):,}円",
                ]
                if pop:
                    lines.append("・人気のため在庫状況次第で上振れの可能性あり✨")
                if tips:
                    lines.append(f"・確認ポイント：{tips}")
                lines.append("\n🎁 LINE友だち限定：査定金額から +500円UP クーポン適用中")
                lines.append("\nこのまま続けますか？")

                quick = [
                    {"type": "action", "action": {"type": "message", "label": "正確なスタッフ査定", "text": "スタッフ査定を希望"}},
                    {"type": "action", "action": {"type": "message", "label": "出張買取を希望", "text": "出張買取を依頼"}},
                    {"type": "action", "action": {"type": "message", "label": "店舗に持ち込み", "text": "店舗持ち込みを希望"}},
                ]
                reply_text_with_quick(reply_token, "\n".join(lines), quick)
                continue

            # ---------- 画像 ----------
            elif mtype == "image":
                # 画像取得 → サイズ上限チェック
                img_bytes = get_line_image_bytes(msg.get("id"))
                if len(img_bytes) > IMG_MAX_BYTES:
                    reply_text(reply_token, "画像が大きすぎます（2MB以内で送信してください）。\n型番ラベルを接写すると精度が上がります。")
                    continue

                data = assess_from_text_or_image(image_bytes=img_bytes)

                if data.get("error") in ("disabled", "quota"):
                    reply_text(
                        reply_token,
                        "現在、AI査定がご利用いただけません。\n"
                        "・型番ラベルにピントを合わせた写真を送ってください（スタッフが手動で査定）\n"
                        "・または「出張買取を依頼」を選んで仮予約できます。"
                    )
                    continue

                if data.get("error"):
                    app.logger.warning(f"assess error: {data}")
                    reply_text(reply_token, "画像の解析に失敗しました。型番ラベルにピントを合わせてもう一度送ってください。")
                    continue

                low, high = data.get("estimate_low"), data.get("estimate_high")
                cat, brand, model = data.get("category", ""), data.get("brand", ""), data.get("model", "")
                tips = data.get("tips", "")
                pop = data.get("popularity_hint", False)

                lines = [
                    "📸 画像を確認しました。仮査定の買取目安です。",
                    f"・商品：{cat} / {brand} {model}".strip(" /"),
                    f"・買取目安：{int(low):,}円 〜 {int(high):,}円",
                ]
                if pop:
                    lines.append("・人気のため在庫状況次第で上振れの可能性あり✨")
                if tips:
                    lines.append(f"・確認ポイント：{tips}")
                lines.append("\n🎁 LINE友だち限定：査定金額から +500円UP クーポン適用中")
                lines.append("\nこのまま続けますか？")

                quick = [
                    {"type": "action", "action": {"type": "message", "label": "正確なスタッフ査定", "text": "スタッフ査定を希望"}},
                    {"type": "action", "action": {"type": "message", "label": "出張買取を希望", "text": "出張買取を依頼"}},
                    {"type": "action", "action": {"type": "message", "label": "店舗に持ち込み", "text": "店舗持ち込みを希望"}},
                ]
                reply_text_with_quick(reply_token, "\n".join(lines), quick)
                continue

            # ---------- その他のタイプ ----------
            else:
                reply_text(reply_token, "対応していないメッセージ形式です。テキストまたは画像でお送りください。")
                continue

        # 必ず200で返す（LINE要件）
        return "", 200

    except Exception:
        app.logger.exception("Webhook handler crashed")
        return "", 200


# ---------------------------
# 週1配信用（任意機能）
# ---------------------------
def broadcast_text(text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
        json={"messages": [{"type": "text", "text": text[:5000]}]},
        timeout=15,
    )

@app.route("/admin/broadcast", methods=["POST"])
def admin_broadcast():
    if request.headers.get("X-Admin-Key") != os.getenv("ADMIN_KEY"):
        return "forbidden", 403
    body = request.get_json(force=True) or {}
    items = body.get("items", [])
    msg = "🟢今週の買取強化アイテム\n" + "\n".join([f"・{x}" for x in items]) + \
          "\n\n査定は画像か型番を送るだけ！LINE友だち限定 +500円UP中🎁"
    broadcast_text(msg)
    return "ok", 200


if __name__ == "__main__":
    # ローカル実行用。RenderではProcfileでgunicornが使われます
    app.run(host="0.0.0.0", port=5000)


if __name__ == "__main__":
    # ローカル実行用。RenderではProcfileでgunicornが使われます
    app.run(host="0.0.0.0", port=5000)
