import os
from flask import Flask, request
import requests

# --- OpenAI 新SDK ---
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")

def reply_text(reply_token: str, text: str):
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
                     "Content-Type": "application/json"},
            json={
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": text[:5000]}]  # 念のため長文ガード
            },
            timeout=10
        )
    except Exception:
        # 返信失敗はログに出すだけ（200は返す）
        app.logger.exception("LINE reply failed")

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        body = request.get_json(force=True, silent=True) or {}
        events = body.get("events", [])

        for ev in events:
            if ev.get("type") != "message":
                continue

            msg = ev.get("message", {})
            reply_token = ev.get("replyToken")
            mtype = msg.get("type")

            # テキストだけ先に対応（画像は後で拡張）
            if mtype == "text":
                user_text = msg.get("text", "")
                # --- リッチメニューのボタン処理 ---
                text = user_text.strip()

                if text == "AI査定":
                    reply_text(reply_token,
                        "📸 AI査定を開始します。\n商品の写真（正面や型番ラベル）や型番テキストを送ってください。")
                    continue

                elif text == "お問い合わせ":
                    reply_text(reply_token,
                        "📩 お問い合わせありがとうございます。\n内容をこちらに送信してください。")
                    continue

                elif text == "出張買取を依頼":
                    reply_text(reply_token,
                        "🚛 出張買取の仮予約を開始します。\nご希望の訪問日時をお知らせください。")
                    continue

                try:
                    gpt = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {"role": "system", "content": "あなたはリユースショップの査定担当者です。簡潔・親切に答えてください。"},
                            {"role": "user", "content": user_text}
                        ],
                        timeout=10
                    )
                    answer = gpt.choices[0].message.content or "ありがとうございます。"
                except Exception:
                    app.logger.exception("OpenAI error")
                    answer = "ただいま査定エンジンが混み合っています。内容をもう一度お送りいただくか、少し時間をおいてお試しください。"

                if reply_token:
                    reply_text(reply_token, answer)

            else:
                # 未対応タイプ（画像など）はメッセージ
                if ev.get("replyToken"):
                    reply_text(ev["replyToken"], "画像査定は準備中です。まずは商品名や型番をテキストで送ってください。")

        # ここまで来たら必ず200を返す（LINEの要件）
        return "", 200

    except Exception:
        app.logger.exception("Webhook handler crashed")
        # 例外があっても200を返してLINE側のリトライを防ぐ
        return "", 200

if __name__ == "__main__":
    # ローカル実行用。RenderではProcfileでgunicornが使われます
    app.run(host="0.0.0.0", port=5000)
