# Feishu (Lark) Integration Reference

FastInsight can receive paper screenshots and links from Feishu, but it **does
not forward interactive cards to owners**. Delivery is FastResearch only. This
page documents (1) Feishu as an input channel, (2) long connection mode, and
(3) optional raw API notes if you still need source-chat confirmation.

## 1. Connector-based flow (input only)

1. In WorkBuddy / OpenClaw, enable **飞书 (Feishu)** and link the tenant.
2. Once connected, the bot can **receive** group messages, including images
   (screenshots) and captions. With `groupPolicy: "open"`, `requireMention`
   defaults to false so a follow-up image (which cannot carry an @mention)
   still reaches the agent.
3. Users trigger the bot with a standalone `@fastinsight` (a Feishu
   mention whose name is FastInsight, or the literal text). Do not require a long prompt, and do not require the
   command and screenshot in the same message. The bot arms capture and reads
   the **next** image or paper URL in that chat.
4. After FastResearch publish succeeds, a short confirmation in the **source
   chat** is allowed. Do not send owner cards. Do not 私聊 / DM owners.

Owner `name` in `colleagues.yaml` is the FastResearch `person`. `feishu_open_id`
is optional and unused for delivery.

## 2. Long connection mode (self-hosting)

The self-hosted bridge uses the official `lark-oapi` SDK and a persistent
WebSocket connection. Configure the app in the Feishu developer console:

1. Enable the bot capability and grant message receive/send scopes.
2. Under event subscriptions choose **use long connection to receive events**.
3. Subscribe to `im.message.receive_v1`.
4. Set `FEISHU_APP_ID` and `FEISHU_APP_SECRET`, then run
   `python scripts/feishu_bridge.py`.

No public request URL or `FEISHU_VERIFICATION_TOKEN` is needed. The SDK handles
connection heartbeats and reconnects. The bridge dispatches text/link messages
to `auto_pipeline.py` (FastResearch publish). Image messages require `@fastinsight` first, then the next image is captured
in the same turn via `pending_capture.py wait` (Feishu message list + inbound
media).

## 3. Raw Feishu Open API (optional source-chat confirm)

Use this only if you send a confirmation back to the source group. Do not use
it to forward cards to owners.

### App setup
- Create an app at <https://open.feishu.cn> (international: <https://www.feishu.com>)
  → add the **bot** capability → enable needed scopes **and publish a new version**.
  Checking a scope in 权限管理 does nothing until 版本管理 creates and publishes a version (tenant apps may also need admin review):
  - `im:message` (send the wait prompt and source-chat confirmation)
  - `im:message:receive` (receive group messages, for event subscription)
  - `im:message.group_msg` (**required** to `GET /im/v1/messages` in a group; without it wait cannot see the next unmentioned screenshot)
  - `im:resource` (download image bytes from a message)
- Event subscription: subscribe to `im.message.receive_v1`.
- Optional: enable 「接收群聊中的所有消息」 so unmentioned group images also reach OpenClaw inbound. `requireMention: false` alone is not enough if Feishu never delivers the event.

### Get tenant access token
```
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
Content-Type: application/json
{ "app_id": "<APP_ID>", "app_secret": "<APP_SECRET>" }
```

### Receive a message event (excerpt)
```json
{
  "event": {
    "message": {
      "message_type": "image",
      "content": "{\"image_key\":\"img_xxxx\"}",
      "chat_id": "oc_xxxx",
      "sender": { "sender_id": { "open_id": "ou_xxxx" }, "name": "张三" }
    }
  }
}
```
- For an `image` message, download it via
  `GET /open-apis/im/v1/messages/{message_id}/resources/{image_key}?type=image`
  (fallback: `GET /open-apis/im/v1/images/{image_key}`) after `@fastinsight`
  starts `wait`. Listing uses
  `GET /open-apis/im/v1/messages?container_id_type=chat&container_id=...`.
  A 400 with Feishu `code=230027` and `need scope: im:message.group_msg` means
  the app cannot read group history; `pending_capture.py` keeps that `code/msg`
  in `warnings` / `missing_scopes` instead of a bare `HTTP Error 400`.

### Optional confirmation back to the group
Same endpoint with `receive_id` = the group `chat_id` and
`receive_id_type=chat_id`:
```json
{ "receive_id": "<chat_id>", "msg_type": "text",
  "content": "{\"text\":\"✅ 已将《标题》分拣至【方向】，并已发布至 FastResearch。\"}" }
```

`scripts/send_feishu_card.py` now refuses to send. `assets/feishu_card.json`
remains on disk for compatibility only. The current skill must not send owner
cards or owner DMs.
