# Feishu (Lark) Integration Reference

FastInsight assumes **WorkBuddy is the bot**, connected to Feishu via the Feishu
connector. This page documents (1) the connector-based flow used by the skill, and
(2) the raw Feishu Open API for teams that want to self-host instead.

## 1. Connector-based flow (recommended)

1. In WorkBuddy, open the connector panel and enable **飞书 (Feishu)**. Follow the
   OAuth/login prompt to link your Feishu account / tenant.
2. Once connected, WorkBuddy can:
   - **Receive** messages posted to a group where the bot is a member, including
     images (screenshots) and their captions.
   - **Send** messages to a user or group, addressed by `open_id` (the stable
     per-tenant user id), or by `chat_id` for groups.
3. When a screenshot arrives, follow the SKILL.md workflow: read the image →
   `verify_paper.py` → `analyze_trends.py` → `route_paper.py` → send the card to each matched owner's
   `open_id` → confirm in the group.

### Getting a teammate's `open_id`

`open_id` is required for routing. Common ways to obtain it:

- **Feishu admin console** → Contacts → user → the id shown in the URL / detail
  panel (starts with `ou_`).
- **Bot API**: call `GET /open-apis/contact/v3/users` (needs contact:user
  read scope) and map by name/email to `open_id`.
- **Simplest for a small team**: ask each person to @-mention the bot once; the
  bot's incoming message event includes their `sender.sender_id.open_id`. Record
  those into `colleagues.yaml`.

## 2. Raw Feishu Open API (self-hosting)

Use this only if you run your own service instead of the connector.

### App setup
- Create an app at <https://open.feishu.cn> (international: <https://www.feishu.com>)
  → add the **bot** capability → enable needed scopes:
  - `im:message` (send messages)
  - `im:message:receive` (receive group messages, for event subscription)
  - `contact:user.base:readonly` (lookup open_id by name)
- Event subscription: subscribe to `im.message.receive_v1`. Feishu will POST
  message events to your `request_url`; verify the URL with the challenge handshake.

### Get tenant access token
```
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
Content-Type: application/json
{ "app_id": "<APP_ID>", "app_secret": "<APP_SECRET>" }
# -> { "tenant_access_token": "...", "expire": 7200 }
```
Pass it as `Authorization: Bearer <token>` on subsequent calls.

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
  `GET /open-apis/im/v1/images/{image_key}?type=message` (header
  `Authorization: Bearer <token>`). Then run the image through the multimodal
  read step.

### Send an interactive card to a user
```
POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id
Authorization: Bearer <token>
Content-Type: application/json
{
  "receive_id": "<owner_open_id>",
  "msg_type": "interactive",
  "content": "<the JSON card from feishu_card.json, as a stringified JSON>"
}
```
Note: `content` must be a **JSON string**, not an object — `json.dumps(card)`.

### Send a confirmation back to the group
Same endpoint with `receive_id` = the group `chat_id` and `receive_id_type=chat_id`,
or `msg_type: "text"`:
```json
{ "receive_id": "<chat_id>", "msg_type": "text",
  "content": "{\"text\":\"✅ 已将《标题》分拣至【方向】，转发给 @张三、@李四。\"}" }
```

## 3. Card shape

The forward card is an interactive card (see `assets/feishu_card.json`). Key
fields: `header.template` (color), `elements[].tag` (`div`, `note`, `action`),
and a `button` action with `url` to open the paper. Buttons open URLs in a new
tab; they do not require extra scopes.
