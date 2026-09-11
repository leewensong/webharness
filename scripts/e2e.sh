#!/usr/bin/env bash
# WebHarness 端到端验证：人类注册/登录、Agent 密钥登录、房间可见性、权限、附件。
# 用法: ./scripts/e2e.sh [base_url]   （默认 http://127.0.0.1:8765）
set -euo pipefail

URL="${1:-http://127.0.0.1:8765}"
SUF="${RANDOM}${RANDOM}"
HUMAN="e2e-human-$SUF"
AGENT="e2e-agent-$SUF"
PRIV_ROOM="e2e-priv-$SUF"
PUB_ROOM="e2e-pub-$SUF"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

J() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
PASS=0; FAIL=0
check() { # check <描述> <实际> <期望包含>
  if [[ "$2" == *"$3"* ]]; then echo "PASS  $1"; PASS=$((PASS+1));
  else echo "FAIL  $1"; echo "      实际: $2"; FAIL=$((FAIL+1)); fi
}

echo "== 探活 =="
check "health" "$(curl -sS "$URL/api/health")" '"ok":true'

echo "== 人类注册登录 =="
python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1],"password":"pass123"}))' "$HUMAN" > "$TMP/user.json"
curl -sS "$URL/api/users" -H 'Content-Type: application/json' -d @"$TMP/user.json" >/dev/null
HTOK=$(curl -sS "$URL/api/login" -H 'Content-Type: application/json' -d @"$TMP/user.json" | J "['token']")
check "重复注册 409" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/users" -H 'Content-Type: application/json' -d @"$TMP/user.json")" "409"
python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1],"password":"wrong"}))' "$HUMAN" > "$TMP/bad.json"
check "密码错误 401" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/login" -H 'Content-Type: application/json' -d @"$TMP/bad.json")" "401"

echo "== Agent 接入 =="
openssl genpkey -algorithm ed25519 -out "$TMP/priv.pem" 2>/dev/null
openssl pkey -in "$TMP/priv.pem" -pubout -out "$TMP/pub.pem" 2>/dev/null
python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1],"publicKey":open(sys.argv[2]).read()}))' "$AGENT" "$TMP/pub.pem" > "$TMP/agent.json"
curl -sS "$URL/api/agents" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d @"$TMP/agent.json" >/dev/null
check "非法公钥 400" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/agents" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"username":"e2e-bad","publicKey":"nope"}')" "400"

agent_login() {
  local nonce sig
  nonce=$(curl -sS "$URL/api/agent-auth/challenge" -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1]}))' "$AGENT")" | J "['nonce']")
  printf '%s' "$nonce" > "$TMP/n.txt"
  sig=$(openssl pkeyutl -sign -inkey "$TMP/priv.pem" -rawin -in "$TMP/n.txt" | base64)
  curl -sS "$URL/api/agent-auth/login" -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1],"signature":sys.argv[2]}))' "$AGENT" "$sig")"
}
ATOK=$(agent_login | J "['token']")
check "agent me" "$(curl -sS "$URL/api/me" -H "Authorization: Bearer $ATOK")" '"kind":"agent"'
NONCE=$(curl -sS "$URL/api/agent-auth/challenge" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1]}))' "$AGENT")" | J "['nonce']")
printf '%s' "$NONCE" > "$TMP/n.txt"
SIG=$(openssl pkeyutl -sign -inkey "$TMP/priv.pem" -rawin -in "$TMP/n.txt" | base64)
python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1],"signature":sys.argv[2]}))' "$AGENT" "$SIG" > "$TMP/login.json"
curl -sS "$URL/api/agent-auth/login" -H 'Content-Type: application/json' -d @"$TMP/login.json" >/dev/null
check "nonce 重放 401" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/agent-auth/login" -H 'Content-Type: application/json' -d @"$TMP/login.json")" "401"
check "agent 管理 agent 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/agents" -H "Authorization: Bearer $ATOK")" "403"

echo "== 房间可见性 =="
python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"visibility":"public"}))' "$PUB_ROOM" > "$TMP/pub_room.json"
python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"password":"pw123"}))' "$PRIV_ROOM" > "$TMP/priv_room.json"
python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1]}))' "$PRIV_ROOM" > "$TMP/priv_nopw.json"
python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"password":"no"}))' "$PRIV_ROOM" > "$TMP/priv_badpw.json"
python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1]}))' "$PUB_ROOM" > "$TMP/pub_join.json"
curl -sS "$URL/api/rooms" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d @"$TMP/pub_room.json" >/dev/null
curl -sS "$URL/api/rooms" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d @"$TMP/priv_room.json" >/dev/null
# 历史消息必须在 agent 加入之前发送，才能验证「加入前历史」水位
curl -sS "$URL/api/rooms/$PUB_ROOM/messages" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"content":"历史-加入前"}' >/dev/null
check "public 列表含公开房" "$(curl -sS "$URL/api/rooms/public" -H "Authorization: Bearer $ATOK")" "$PUB_ROOM"
check "public 列表不含私密房" "$(curl -sS "$URL/api/rooms/public" -H "Authorization: Bearer $ATOK" | python3 -c "import sys,json;print('$PRIV_ROOM' in [r['roomName'] for r in json.load(sys.stdin)['rooms']])")" "False"
check "我的房间含两个房" "$(curl -sS "$URL/api/rooms" -H "Authorization: Bearer $HTOK" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['rooms']))")" "2"

AGENT_ROOM="e2e-abot-$SUF"
OTHER="e2e-other-$SUF"

echo "== 主人可见自己 Agent 的房间 =="
python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"password":"agentpw","visibility":"private"}))' "$AGENT_ROOM" > "$TMP/agent_priv.json"
python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1]}))' "$AGENT_ROOM" > "$TMP/agent_priv_nopw.json"
curl -sS "$URL/api/rooms" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d @"$TMP/agent_priv.json" >/dev/null
check "主人列表含 agent 私密房" "$(curl -sS "$URL/api/rooms" -H "Authorization: Bearer $HTOK")" "$AGENT_ROOM"
check "public 仍不含 agent 私密房" "$(curl -sS "$URL/api/rooms/public" -H "Authorization: Bearer $HTOK" | python3 -c "import sys,json;print('$AGENT_ROOM' in [r['roomName'] for r in json.load(sys.stdin)['rooms']])")" "False"
check "主人免密加入自己 agent 的私密房" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d @"$TMP/agent_priv_nopw.json")" "200"
python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1],"password":"pass123"}))' "$OTHER" > "$TMP/other.json"
curl -sS "$URL/api/users" -H 'Content-Type: application/json' -d @"$TMP/other.json" >/dev/null
OTOK=$(curl -sS "$URL/api/login" -H 'Content-Type: application/json' -d @"$TMP/other.json" | J "['token']")
check "外人列表不含 agent 私密房" "$(curl -sS "$URL/api/rooms" -H "Authorization: Bearer $OTOK" | python3 -c "import sys,json;print('$AGENT_ROOM' in [r['roomName'] for r in json.load(sys.stdin)['rooms']])")" "False"
check "外人无密码加入 agent 私密房 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d @"$TMP/agent_priv_nopw.json")" "403"

echo "== 加入与密码 =="
check "私密房无密码 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d @"$TMP/priv_nopw.json")" "403"
check "私密房错密码 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d @"$TMP/priv_badpw.json")" "403"
check "加入成功带 onlineUsers" "$(curl -sS "$URL/api/rooms" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d @"$TMP/priv_room.json")" "onlineUsers"
check "公开房免密码加入" "$(curl -sS "$URL/api/rooms" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d @"$TMP/pub_join.json" | J "['joined']")" "True"

echo "== 消息与权限 =="
curl -sS "$URL/api/rooms/$PUB_ROOM/messages" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"content":"agent 在公开房发言"}' >/dev/null
check "他人新消息计入未读" "$(curl -sS "$URL/api/rooms" -H "Authorization: Bearer $HTOK" | python3 -c "import sys,json; rooms=json.load(sys.stdin)['rooms']; r=next(x for x in rooms if x['roomName']=='$PUB_ROOM'); print(r.get('unreadCount',0)>=1)")" "True"
check "默认可看历史" "$(curl -sS "$URL/api/rooms/$PUB_ROOM/messages" -H "Authorization: Bearer $ATOK")" "历史-加入前"
check "读过之后未读清零" "$(curl -sS "$URL/api/rooms" -H "Authorization: Bearer $ATOK" | python3 -c "import sys,json; rooms=json.load(sys.stdin)['rooms']; r=next(x for x in rooms if x['roomName']=='$PUB_ROOM'); print(r.get('unreadCount',0))")" "0"
curl -sS -X PUT "$URL/api/rooms/$PUB_ROOM/permissions/$AGENT" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"canViewHistory": false}' >/dev/null
check "禁历史后只见新消息" "$(curl -sS "$URL/api/rooms/$PUB_ROOM/messages" -H "Authorization: Bearer $ATOK" | python3 -c "import sys,json;print(any('历史' in m['content'] for m in json.load(sys.stdin)['messages']))")" "False"
curl -sS -X PUT "$URL/api/rooms/$PUB_ROOM/permissions/$AGENT" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"canSpeak": false}' >/dev/null
check "禁言后发言 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$PUB_ROOM/messages" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"content":"x"}')" "403"
curl -sS -X PATCH "$URL/api/rooms/$PUB_ROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"muted": true}' >/dev/null
check "全体禁言时房主仍可发言" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$PUB_ROOM/messages" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"content":"房主发言"}')" "200"
curl -sS -X PATCH "$URL/api/rooms/$PUB_ROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"muted": false}' >/dev/null
curl -sS -X PUT "$URL/api/rooms/$PUB_ROOM/permissions/$AGENT" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"canSpeak": true}' >/dev/null

echo "== 流式回复 =="
START=$(curl -sS -X POST "$URL/api/rooms/$PUB_ROOM/messages/stream" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"content":"Hel"}')
check "开始流式" "$START" '"streaming":true'
SID=$(printf '%s' "$START" | J "['id']")
check "追加 delta" "$(curl -sS -X POST "$URL/api/rooms/$PUB_ROOM/messages/$SID/stream" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"delta":"lo"}')" '"Hello"'
check "streamIds 能拿到更新" "$(curl -sS "$URL/api/rooms/$PUB_ROOM/messages?afterId=$SID&streamIds=$SID" -H "Authorization: Bearer $ATOK")" "Hello"
check "结束流式" "$(curl -sS -X POST "$URL/api/rooms/$PUB_ROOM/messages/$SID/stream" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"done":true}')" '"streaming":false'
check "非作者更新 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$PUB_ROOM/messages/$SID/stream" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"delta":"x"}')" "403"
check "结束后再追加 409" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$PUB_ROOM/messages/$SID/stream" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"delta":"x"}')" "409"
python3 -c 'import json;print(json.dumps({"content":"a"*8001}))' > "$TMP/too_long.json"
check "超长 422" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$PUB_ROOM/messages/stream" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d @"$TMP/too_long.json")" "422"

echo "== 附件 =="
echo "e2e attachment" > "$TMP/a.txt"
check "agent 上传附件" "$(curl -sS -X POST "$URL/api/rooms/$PUB_ROOM/attachments" -H "Authorization: Bearer $ATOK" -F "file=@$TMP/a.txt")" "attachmentName"
MID=$(curl -sS "$URL/api/rooms/$PUB_ROOM/messages" -H "Authorization: Bearer $ATOK" | python3 -c "import sys,json;print([m['id'] for m in json.load(sys.stdin)['messages'] if m['msgType']=='attachment'][0])")
check "房主下载附件" "$(curl -sS "$URL/api/rooms/$PUB_ROOM/attachments/$MID" -H "Authorization: Bearer $HTOK")" "e2e attachment"
python3 - <<'PY' > "$TMP/dot.png"
import base64, sys
sys.stdout.buffer.write(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
))
PY
check "上传图片 msgType=image" "$(curl -sS -X POST "$URL/api/rooms/$PUB_ROOM/attachments" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/dot.png")" '"msgType":"image"'
curl -sS -X PUT "$URL/api/rooms/$PUB_ROOM/permissions/$AGENT" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"canUpload": false}' >/dev/null
check "禁上传后 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$PUB_ROOM/attachments" -H "Authorization: Bearer $ATOK" -F "file=@$TMP/a.txt")" "403"

echo "== 房主管理与归档 =="
check "非房主改名 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$PRIV_ROOM" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"roomName":"x"}')" "403"
curl -sS "$URL/api/rooms/$PRIV_ROOM/messages" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"content":"归档前留言"}' >/dev/null
ARCH_JSON=$(curl -sS -X POST "$URL/api/rooms/$PRIV_ROOM/archive" -H "Authorization: Bearer $HTOK")
check "归档返回 archived" "$ARCH_JSON" '"archived":true'
ARCH_ID=$(printf '%s' "$ARCH_JSON" | J "['roomId']")
check "活动列表不含已归档房" "$(curl -sS "$URL/api/rooms" -H "Authorization: Bearer $HTOK" | python3 -c "import sys,json;print('$PRIV_ROOM' in [r['roomName'] for r in json.load(sys.stdin)['rooms']])")" "False"
check "归档列表含该房" "$(curl -sS "$URL/api/archives" -H "Authorization: Bearer $HTOK")" "$PRIV_ROOM"
check "归档消息可查" "$(curl -sS "$URL/api/archives/$ARCH_ID/messages" -H "Authorization: Bearer $HTOK")" "归档前留言"
REJOIN=$(curl -sS "$URL/api/rooms" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d @"$TMP/priv_room.json")
check "同名可再建" "$(printf '%s' "$REJOIN" | J "['created']")" "True"
NEW_ID=$(printf '%s' "$REJOIN" | J "['roomId']")
check "新房 id 不同" "$(python3 -c "print('$NEW_ID'!='$ARCH_ID')")" "True"
check "旧归档记录仍在" "$(curl -sS "$URL/api/archives/$ARCH_ID/messages" -H "Authorization: Bearer $HTOK")" "归档前留言"
check "外人看归档 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/archives/$ARCH_ID" -H "Authorization: Bearer $OTOK")" "403"
check "非房主归档 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$PUB_ROOM/archive" -H "Authorization: Bearer $ATOK")" "403"

echo "== 头像（2D）=="
JPG_B64='/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=='
printf '%s' "$JPG_B64" | base64 -d > "$TMP/a.jpg"
python3 -c "open('$TMP/big.jpg','wb').write(open('$TMP/a.jpg','rb').read() + b'\x00'*(1100*1024))"
printf 'not an image' > "$TMP/bad.jpg"
AVH="e2e-avatar-$SUF"
python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1],"password":"pass123","avatar":"data:image/jpeg;base64,"+sys.argv[2]}))' "$AVH" "$JPG_B64" > "$TMP/av.json"
check "注册带头像返回 avatarUrl" "$(curl -sS "$URL/api/users" -H 'Content-Type: application/json' -d @"$TMP/av.json")" "/avatar?v="
AVTOK=$(curl -sS "$URL/api/login" -H 'Content-Type: application/json' -d @"$TMP/av.json" | J "['token']")
check "上传的头像按 jpeg 返回" "$(curl -sS -o /dev/null -w '%{content_type}' "$URL/api/users/$AVH/avatar" -H "Authorization: Bearer $AVTOK")" "image/jpeg"
check "未上传则返回 SVG 缺省头像" "$(curl -sS -o /dev/null -w '%{content_type}' "$URL/api/users/$HUMAN/avatar" -H "Authorization: Bearer $HTOK")" "image/svg+xml"
check "头像超过 1MB 413" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/me/avatar" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/big.jpg")" "413"
check "非图片头像 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/me/avatar" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/bad.jpg")" "400"
check "注册头像格式错误 400" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/users" -H 'Content-Type: application/json' -d '{"username":"e2e-badav-'$SUF'","password":"pass123","avatar":"data:image/jpeg;base64,!!!!"}')" "400"
check "上传头像 200" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/me/avatar" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/a.jpg")" "200"
check "删除头像后回落 SVG" "$(curl -sS -o /dev/null -X DELETE "$URL/api/me/avatar" -H "Authorization: Bearer $HTOK"; curl -sS -o /dev/null -w '%{content_type}' "$URL/api/users/$HUMAN/avatar" -H "Authorization: Bearer $HTOK")" "image/svg+xml"

echo "== 形象归属（?as=）=="
check "主人替名下 Agent 传头像 200" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/me/avatar?as=$AGENT" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/a.jpg")" "200"
check "外人替该 Agent 传头像 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/me/avatar?as=$AGENT" -H "Authorization: Bearer $OTOK" -F "file=@$TMP/a.jpg")" "403"
check "as= 指向人类账号 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/me/avatar?as=$OTHER" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/a.jpg")" "403"

echo "== 3D 形象 =="
printf 'nope' > "$TMP/bad.glb"
python3 -c "open('$TMP/m.glb','wb').write(b'glTF\x02\x00\x00\x00' + b'\x00'*64)"
check "非 GLB 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/bad.glb")" "400"
check "上传 GLB 200" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/m.glb")" "200"
check "GLB 下载类型正确" "$(curl -sS -o /dev/null -w '%{content_type}' "$URL/api/users/$HUMAN/model3d" -H "Authorization: Bearer $HTOK")" "model/gltf-binary"
check "/api/me 读回 model3dUrl" "$(curl -sS "$URL/api/me" -H "Authorization: Bearer $HTOK" | python3 -c "import sys,json;print(json.load(sys.stdin)['model3dUrl'] or '')")" "/model3d?v="
check "PUT 外链并标记 ARKit" "$(curl -sS -X PUT "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"url":"https://cdn.example.com/m.glb","arkit":true}' | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['model3dUrl'],d['model3dArkit'])")" "https://cdn.example.com/m.glb True"
check "DELETE 清空 3D" "$(curl -sS -X DELETE "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['model3dUrl'],d['model3dArkit'])")" "None False"

echo "== 房间 rules 与 room agent =="
RULE_ROOM="e2e-rules-$SUF"
python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"rules":"不许刷屏","roomAgent":sys.argv[2]}))' "$RULE_ROOM" "$AGENT" > "$TMP/rules.json"
RINFO=$(curl -sS "$URL/api/rooms" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d @"$TMP/rules.json")
check "建房返回 rules" "$RINFO" "不许刷屏"
check "建房返回 roomAgent" "$(printf '%s' "$RINFO" | python3 -c "import sys,json;print(json.load(sys.stdin)['roomAgent'])")" "$AGENT"
check "详情读回 rules" "$(curl -sS "$URL/api/rooms/$RULE_ROOM" -H "Authorization: Bearer $HTOK" | python3 -c "import sys,json;print(json.load(sys.stdin)['rules'])")" "不许刷屏"
check "改 rules 生效" "$(curl -sS -X PATCH "$URL/api/rooms/$RULE_ROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"rules":"新规则"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['rules'])")" "新规则"
check "传空串清空 roomAgent" "$(curl -sS -X PATCH "$URL/api/rooms/$RULE_ROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"roomAgent":""}' | python3 -c "import sys,json;print(json.load(sys.stdin)['roomAgent'])")" "None"
python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"roomAgent":sys.argv[2]}))' "e2e-rules-x-$SUF" "$AGENT" > "$TMP/rx.json"
python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"roomAgent":sys.argv[2]}))' "e2e-rules-y-$SUF" "$OTHER" > "$TMP/ry.json"
check "别人的 Agent 当 roomAgent 400" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d @"$TMP/rx.json")" "400"
check "非 Agent 当 roomAgent 400" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d @"$TMP/ry.json")" "400"

echo "== 头像随消息 / 成员下发 =="
check "消息带 avatarUrl" "$(curl -sS "$URL/api/rooms/$PUB_ROOM/messages" -H "Authorization: Bearer $HTOK")" "avatarUrl"
check "成员列表带头像" "$(curl -sS "$URL/api/rooms/$PUB_ROOM/members" -H "Authorization: Bearer $HTOK")" "avatarUrl"
check "在线列表带头像" "$(curl -sS "$URL/api/rooms/$PUB_ROOM" -H "Authorization: Bearer $HTOK")" "avatarUrl"
check "Agent 列表带头像" "$(curl -sS "$URL/api/agents" -H "Authorization: Bearer $HTOK")" "avatarUrl"

echo "== Agent 停用 =="
curl -sS -X PATCH "$URL/api/agents/$AGENT" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"status":"disabled"}' >/dev/null
check "停用后取挑战 401" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/agent-auth/challenge" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1]}))' "$AGENT")")" "401"

echo
echo "通过 $PASS 项，失败 $FAIL 项"
[[ $FAIL -eq 0 ]]
