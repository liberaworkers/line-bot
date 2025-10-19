import os
from flask import Flask, request
import requests

# --- OpenAI 新SDK ---
from openai import OpenAI
import base64, json  # ←これを追加

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")

def reply_text(reply_token: str, text: str):
    def reply_text_with_quick(reply_token: str, text: str, quick_items=None):
    body = {
        "replyToken": reply_token,
        "messages": [{
            "type": "text",
            "text": text[:5000],
            **({"quickReply": {"items": quick_items}} if quick_items else {})
        }]
    }
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
        json=body, timeout=10
    )
def get_line_image_bytes(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    r = requests.get(url, headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}, timeout=20)
    r.raise_for_status()
    return r.content
ASSESS_SYSTEM = (
    "あなたはリユースショップの査定担当AIです。出力は必ずJSONのみ。"
    "ユーザーに見せるのは『買取目安金額』だけ。中古相場や新品価格の金額は表示しない。"
    "内部ロジックとして、中古相場×0.1~0.2 または 新品価格×0.05~0.1 を目安に計算してよいが、"
    "その根拠や相場の金額は一切表示しない。"
    "項目: category, brand, model, estimate_low, estimate_high, popularity_hint, tips。"
    "estimate_* はJPYの整数。popularity_hint は true/false（SOLDOUTが多い等で人気・上振れ余地があればtrue）。"
    "tips は確認ポイント（傷/付属品/動作など）を簡潔に。"
)

def assess_from_text_or_image(user_text: str = "", image_bytes: bytes | None = None) -> dict:
    content = []
    if user_text:
        content.append({"type": "text", "text": f"対象情報:\n{user_text}"})
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role":"system","content":ASSESS_SYSTEM},
                  {"role":"user","content":content}],
        temperature=0.2, timeout=25
    )
    txt = resp.choices[0].message.content
    try:
        return json.loads(txt)
    except Exception:
        return {"error":"parse_failed","raw":txt}

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
                elif mtype == "image":
    ...

                user_text = msg.get("text", "")
                    # --- 型番/商品名っぽいテキストなら “買取目安のみ” を即時回答 ---
    text = user_text.strip()
    if text and text not in ("AI査定", "お問い合わせ", "出張買取を依頼"):
        try:
            data = assess_from_text_or_image(user_text=text)
            if "error" in data:
                raise RuntimeError("parse failed")

            low, high = data.get("estimate_low"), data.get("estimate_high")
            cat, brand, model = data.get("category",""), data.get("brand",""), data.get("model","")
            tips = data.get("tips","")
            pop = data.get("popularity_hint", False)

            lines = [
                "🧮 仮査定の買取目安です。",
                f"・商品：{cat} / {brand} {model}".strip(" /"),
                f"・買取目安：{int(low):,}円 〜 {int(high):,}円"
            ]
            if pop:
                lines.append("・人気のため在庫状況次第で上振れの可能性あり✨")
            if tips:
                lines.append(f"・確認ポイント：{tips}")
            lines.append("\n🎁 LINE友だち限定：査定金額から +500円UP クーポン適用中")
            lines.append("\nこのまま続けますか？")

            quick = [
              {"type":"action","action":{"type":"message","label":"正確なスタッフ査定","text":"スタッフ査定を希望"}},
              {"type":"action","action":{"type":"message","label":"出張買取を希望","text":"出張買取を依頼"}},
              {"type":"action","action":{"type":"message","label":"店舗に持ち込み","text":"店舗持ち込みを希望"}}
            ]
            reply_text_with_quick(reply_token, "\n".join(lines), quick)
            continue  # ここでこのイベント処理を終了
        except Exception:
            app.logger.exception("text assess failed")
            reply_text(reply_token, "うまく解析できませんでした。写真や型番ラベルの画像も送ってください。")
            continue

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
def broadcast_text(text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
        json={"messages":[{"type":"text","text":text[:5000]}]},
        timeout=15
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
