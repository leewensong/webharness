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
check "PUT 外链并标记 ARKit+Humanoid" "$(curl -sS -X PUT "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"url":"https://cdn.example.com/m.glb","arkit":true,"humanoid":true}' | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['model3dUrl'],d['model3dArkit'],d['model3dHumanoid'])")" "https://cdn.example.com/m.glb True True"
check "改 Humanoid 标记不动外链" "$(curl -sS -X PUT "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"humanoid":false}' | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['model3dHumanoid'],d['model3dUrl'])")" "False https://cdn.example.com/m.glb"
check "DELETE 清空 3D" "$(curl -sS -X DELETE "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['model3dUrl'],d['model3dArkit'],d['model3dHumanoid'])")" "None False False"

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

echo "== roomAgent 治理权 =="
GOV_ROOM="e2e-gov-$SUF"
curl -sS "$URL/api/rooms" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1]}))' "$GOV_ROOM")" >/dev/null
check "PATCH 设 roomAgent" "$(curl -sS -X PATCH "$URL/api/rooms/$GOV_ROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"roomAgent":sys.argv[1]}))' "$AGENT")" | python3 -c "import sys,json;print(json.load(sys.stdin)['roomAgent'])")" "$AGENT"
curl -sS "$URL/api/rooms" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d "$(python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1]}))' "$GOV_ROOM")" >/dev/null
curl -sS "$URL/api/rooms" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d "$(python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1]}))' "$GOV_ROOM")" >/dev/null
check "roomAgent 读成员列表" "$(curl -sS "$URL/api/rooms/$GOV_ROOM/members" -H "Authorization: Bearer $ATOK")" '"members"'
check "普通人 PATCH 房间 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$GOV_ROOM" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d '{"rules":"x"}')" "403"
check "roomAgent PATCH 房间 200" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$GOV_ROOM" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"rules":"治理规则"}')" "200"
check "roomAgent 禁言普通人" "$(curl -sS "$URL/api/rooms/$GOV_ROOM/permissions/$OTHER" -X PUT -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"canSpeak":false}' | python3 -c "import sys,json;print(json.load(sys.stdin)['canSpeak'])")" "False"
check "被禁言发消息 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d '{"content":"hi"}')" "403"
check "被禁言仍可私聊 200" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d "{\"content\":\"@@$HUMAN 悄悄话\"}")" "200"
check "roomAgent 解禁" "$(curl -sS "$URL/api/rooms/$GOV_ROOM/permissions/$OTHER" -X PUT -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"canSpeak":true}' | python3 -c "import sys,json;print(json.load(sys.stdin)['canSpeak'])")" "True"
check "解禁后发消息 200" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d '{"content":"hi"}')" "200"
check "roomAgent 全体禁言" "$(curl -sS -X PATCH "$URL/api/rooms/$GOV_ROOM" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"muted":true}' | python3 -c "import sys,json;print(json.load(sys.stdin)['muted'])")" "True"
check "全体禁言下普通人 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d '{"content":"hi2"}')" "403"
check "全体禁言下私聊也 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d "{\"content\":\"@@$HUMAN 悄悄话\"}")" "403"
check "全体禁言下 roomAgent 200" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"content":"主持"}')" "200"
curl -sS -X PATCH "$URL/api/rooms/$GOV_ROOM" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"muted":false}' >/dev/null
check "roomAgent 加 deny */*" "$(curl -sS "$URL/api/rooms/$GOV_ROOM/whisper-rules" -X POST -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  -d '{"listType":"deny","priority":0,"sender":"*","receiver":"*"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['listType'])")" "deny"
curl -sS "$URL/api/rooms/$GOV_ROOM/whisper-rules" -X POST -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"listType":"allow","priority":10,"sender":sys.argv[1],"receiver":"*"}))' "$AGENT")" >/dev/null
check "禁后普通人私聊 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d "{\"content\":\"@@$HUMAN 悄悄话\"}")" "403"
check "roomAgent 私聊不受限" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d "{\"content\":\"@@$OTHER 提示\"}")" "200"
curl -sS "$URL/api/rooms/$GOV_ROOM/whisper-rules" -X POST -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"listType":"allow","priority":10,"sender":sys.argv[1],"receiver":sys.argv[2]}))' "$OTHER" "$HUMAN")" >/dev/null
WID=$(curl -sS "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d "{\"content\":\"@@$HUMAN 狼人刀3号\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
check "roomAgent 看到他人私聊" "$(curl -sS "$URL/api/rooms/$GOV_ROOM/messages?limit=5" -H "Authorization: Bearer $ATOK")" "狼人刀3号"
check "房主仍能看到他人私聊" "$(curl -sS "$URL/api/rooms/$GOV_ROOM/messages?limit=5" -H "Authorization: Bearer $HTOK")" "狼人刀3号"
check "限制房主 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/permissions/$HUMAN" -X PUT -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"canSpeak":false}')" "403"
check "限制 roomAgent 自己 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/permissions/$AGENT" -X PUT -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"canSpeak":false}')" "403"
check "roomAgent 不能归档 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$GOV_ROOM/archive" -H "Authorization: Bearer $ATOK")" "403"
check "onlineUsers 带 canSpeak" "$(curl -sS "$URL/api/rooms/$GOV_ROOM" -H "Authorization: Bearer $HTOK")" '"canSpeak"'
curl -sS "$URL/api/rooms/$GOV_ROOM/permissions/$OTHER" -X PUT -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"canSpeak":false}' >/dev/null
check "禁言后 canSpeak false" "$(curl -sS "$URL/api/rooms/$GOV_ROOM" -H "Authorization: Bearer $HTOK")" '"canSpeak":false'
curl -sS "$URL/api/rooms/$GOV_ROOM/permissions/$OTHER" -X PUT -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"canSpeak":true}' >/dev/null

check "房主仍能管理" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$GOV_ROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"rules":"still-owner"}')" "200"

echo "== 房间群组（v2.8）=="
check "roomAgent 建群" "$(curl -sS "$URL/api/rooms/$GOV_ROOM/groups" -X POST -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"name":"wolves","members":[sys.argv[1],sys.argv[2]]}))' "$HUMAN" "$AGENT")")" '"name":"wolves"'
check "重复建群 409" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/groups" -X POST -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"name":"wolves","members":[]}')" "409"
check "非治理者建群 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/groups" -X POST -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d '{"name":"villagers","members":[]}')" "403"
check "群成员不在房间 400" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/groups" -X POST -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"name":"x","members":["nobody-here"]}')" "400"
check "成员发 #群组 200" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"content":"#wolves 群聊你好"}')" "200"
CNT_IN=$(curl -sS "$URL/api/rooms/$GOV_ROOM/messages?limit=10" -H "Authorization: Bearer $HTOK" | python3 -c "import sys,json;print(sum('群聊你好' in (m.get('content') or '') for m in json.load(sys.stdin)['messages']))")
check "群内成员(房主)能看到" "$CNT_IN" "1"
CNT_OUT=$(curl -sS "$URL/api/rooms/$GOV_ROOM/messages?limit=10" -H "Authorization: Bearer $OTOK" | python3 -c "import sys,json;print(sum('群聊你好' in (m.get('content') or '') for m in json.load(sys.stdin)['messages']))")
check "群外成员看不到" "$CNT_OUT" "0"
check "非成员 GET groups 不含 wolves" "$(curl -sS "$URL/api/rooms/$GOV_ROOM/groups" -H "Authorization: Bearer $OTOK" | grep -c wolves)" "0"
check "群成员 GET groups 含 wolves" "$(curl -sS "$URL/api/rooms/$GOV_ROOM/groups" -H "Authorization: Bearer $HTOK")" '"wolves"'
check "非成员发 #群组 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d '{"content":"#wolves 试试"}')" "403"
curl -sS "$URL/api/rooms/$GOV_ROOM/whisper-rules" -X POST -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"listType":"allow","priority":20,"sender":sys.argv[1],"receiver":"*"}))' "$OTHER")" >/dev/null
check "PATCH 换群成员名单" "$(curl -sS -X PATCH "$URL/api/rooms/$GOV_ROOM/groups/wolves" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"members":[sys.argv[1],sys.argv[2],sys.argv[3]]}))' "$HUMAN" "$AGENT" "$OTHER")")" '"members"'
check "进群后 OTHER 可发 #群组" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d '{"content":"#wolves 收到"}')" "200"
check "非治理者 PATCH 群组 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$GOV_ROOM/groups/wolves" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d '{"members":[]}')" "403"
check "PATCH 不存在的群 404" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$GOV_ROOM/groups/ghost" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"members":[]}')" "404"
check "删群" "$(curl -sS -X DELETE "$URL/api/rooms/$GOV_ROOM/groups/wolves" -H "Authorization: Bearer $ATOK")" '"deleted":true'
check "删群后再发 #群组 400" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$GOV_ROOM/messages" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"content":"#wolves hi"}')" "400"
check "再删群 404" "$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$URL/api/rooms/$GOV_ROOM/groups/wolves" -H "Authorization: Bearer $ATOK")" "404"

echo "== 房间模板（v2.9）=="
TPL_BODY=$(python3 -c '
import json,sys,base64
name=sys.argv[1]
script=base64.b64encode(b"#!/usr/bin/env python3\nprint(\"demo gm\")\n").decode()
print(json.dumps({"name":name,"title":"模板测试","description":"e2e 临时模板","rules":"模板规则A：请听裁判指挥。","scriptName":"gm.py","scriptBase64":script}))' "e2e-tpl-$SUF")
check "建模板" "$(curl -sS "$URL/api/room-templates" -X POST -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d "$TPL_BODY")" '"title":"模板测试"'
check "重复建模板 409" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/room-templates" -X POST -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d "$TPL_BODY")" "409"
check "模板列表含内置 werewolf" "$(curl -sS "$URL/api/room-templates" -H "Authorization: Bearer $ATOK")" '"name":"werewolf"'
check "模板详情 rules" "$(curl -sS "$URL/api/room-templates/e2e-tpl-$SUF" -H "Authorization: Bearer $HTOK")" '模板规则A'
check "未知模板 404" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/room-templates/ghost-$SUF" -H "Authorization: Bearer $HTOK")" "404"
TPLROOM="e2e-tplroom-$SUF"
check "用模板建房（rules 复制）" "$(curl -sS "$URL/api/rooms" -X POST -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"template":sys.argv[2]}))' "$TPLROOM" "e2e-tpl-$SUF")")" '"rules":"模板规则A：请听裁判指挥。"'
check "建房带模板名回显" "$(curl -sS "$URL/api/rooms/$TPLROOM" -H "Authorization: Bearer $HTOK")" '"template":"e2e-tpl-'"$SUF"'"'
check "房间详情回显脚本名" "$(curl -sS "$URL/api/rooms/$TPLROOM" -H "Authorization: Bearer $HTOK")" '"templateScript":"gm.py"'
check "建房显式 rules 优先" "$(curl -sS "$URL/api/rooms" -X POST -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"template":sys.argv[2],"rules":"自己的规则"}))' "e2e-tplroom2-$SUF" "e2e-tpl-$SUF")")" '"rules":"自己的规则"'
check "未知模板建房 404" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms" -X POST -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"template":"ghost-x"}))' "e2e-tplroom3-$SUF")")" "404"
curl -sS "$URL/api/room-templates/e2e-tpl-$SUF/script" -H "Authorization: Bearer $ATOK" -o "$TMP/gm.py"
check "脚本下载内容一致" "$(base64 < "$TMP/gm.py")" "$(python3 -c 'import base64;print(base64.b64encode(b"#!/usr/bin/env python3\nprint(\"demo gm\")\n").decode())')"
check "内置 werewolf 脚本可下载" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/room-templates/werewolf/script" -H "Authorization: Bearer $HTOK")" "200"
check "静态脚本免登录下载" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/scripts/templates/werewolf")" "200"
curl -sS "$URL/scripts/templates/werewolf" -o "$TMP/wf_static.zip"
curl -sS "$URL/api/room-templates/werewolf/script" -H "Authorization: Bearer $HTOK" -o "$TMP/wf_auth.zip"
check "静态与登录下载内容一致" "$(base64 < "$TMP/wf_static.zip")" "$(base64 < "$TMP/wf_auth.zip")"
check "未知模板静态下载 404" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/scripts/templates/ghost-$SUF")" "404"
check "模板 rules 含脚本地址" "$(curl -sS "$URL/api/room-templates/werewolf" -H "Authorization: Bearer $HTOK")" "/scripts/templates/werewolf"
check "rules 占位符已填充" "$(curl -sS "$URL/api/room-templates/werewolf" -H "Authorization: Bearer $HTOK" | grep -c '{{BASE_URL}}')" "0"
check "werewolf 模板建房 rules 含脚本地址" "$(curl -sS "$URL/api/rooms" -X POST -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"template":"werewolf"}))' "e2e-wfroom-$SUF")")" "/scripts/templates/werewolf"
check "非发布者 PATCH 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/room-templates/e2e-tpl-$SUF" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"title":"黑"}')" "403"
check "发布者 PATCH 200" "$(curl -sS -X PATCH "$URL/api/room-templates/e2e-tpl-$SUF" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"description":"已更新"}')" '"description":"已更新"'
check "系统模板 PATCH 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/room-templates/werewolf" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"title":"x"}')" "403"
check "系统模板 DELETE 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$URL/api/room-templates/werewolf" -H "Authorization: Bearer $HTOK")" "403"
check "非发布者 DELETE 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$URL/api/room-templates/e2e-tpl-$SUF" -H "Authorization: Bearer $ATOK")" "403"
check "未登录拿模板列表 401" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/room-templates")" "401"
check "删模板" "$(curl -sS -X DELETE "$URL/api/room-templates/e2e-tpl-$SUF" -H "Authorization: Bearer $HTOK")" '"deleted":true'
check "删后再取 404" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/room-templates/e2e-tpl-$SUF" -H "Authorization: Bearer $HTOK")" "404"
check "删后房间详情 templateScript 置空" "$(curl -sS "$URL/api/rooms/$TPLROOM" -H "Authorization: Bearer $HTOK" | grep -c '"templateScript":null')" "1"

echo "== 房间 3D 场景 =="
# 最小 GLB（校验只看 glTF 魔数）：JSON chunk 里放一行合法 asset 声明
mk_glb() { # mk_glb <路径> [填充字节数]
  python3 -c 'import struct,sys
p=b"{\"asset\":{\"version\":\"2.0\"}}"+b"\x00"*int(sys.argv[2])
p+=b" "*((4-len(p)%4)%4)
b=b"JSON"+struct.pack("<I",len(p))+p
open(sys.argv[1],"wb").write(b"glTF"+struct.pack("<II",2,12+len(b))+b)' "$1" "${2:-0}"
}
SCROOM="e2e-scene-$SUF"
check "内置场景列表" "$(curl -sS "$URL/api/room-scenes" -H "Authorization: Bearer $HTOK")" '"id":"meeting"'
check "内置场景含狼人杀 12 座" "$(curl -sS "$URL/api/room-scenes" -H "Authorization: Bearer $HTOK")" '"seatCount":12'
check "未登录拿场景列表 401" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/room-scenes")" "401"
check "建房带内置场景" "$(curl -sS "$URL/api/rooms" -X POST -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"scene":{"kind":"builtin","id":"meeting"}}))' "$SCROOM")")" '"scene":{"kind":"builtin","id":"meeting"}'
check "房间详情回显场景" "$(curl -sS "$URL/api/rooms/$SCROOM" -H "Authorization: Bearer $HTOK")" '"scene":{"kind":"builtin","id":"meeting"}'
NSCROOM="e2e-noscene-$SUF"
curl -sS "$URL/api/rooms" -X POST -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1]}))' "$NSCROOM")" >/dev/null
check "未设场景的房间 scene 为 null" "$(curl -sS "$URL/api/rooms/$NSCROOM" -H "Authorization: Bearer $HTOK")" '"scene":null'
check "非法内置 id 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$SCROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d '{"scene":{"kind":"builtin","id":"ghost"}}')" "400"
check "PATCH 换外链场景" "$(curl -sS -X PATCH "$URL/api/rooms/$SCROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d '{"scene":{"kind":"url","url":"https://example.com/room.glb"}}')" '"scene":{"kind":"url","url":"https://example.com/room.glb"}'
check "非 http 外链 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$SCROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d '{"scene":{"kind":"url","url":"javascript:alert(1)"}}')" "400"
check "非房主 PATCH 场景 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$SCROOM" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  -d '{"scene":{"kind":"none"}}')" "403"
check "PATCH 清空场景" "$(curl -sS -X PATCH "$URL/api/rooms/$SCROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"scene":{"kind":"none"}}')" '"scene":null'
mk_glb "$TMP/room.glb" 0
check "上传场景 GLB" "$(curl -sS -X POST "$URL/api/rooms/$SCROOM/scene" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/room.glb")" '"scene":{"kind":"file"'
curl -sS "$URL/api/rooms/$SCROOM/scene" -H "Authorization: Bearer $HTOK" -o "$TMP/room_dl.glb"
check "下载场景字节一致" "$(base64 < "$TMP/room_dl.glb")" "$(base64 < "$TMP/room.glb")"
check "场景 file URL 带版本号" "$(curl -sS "$URL/api/rooms/$SCROOM" -H "Authorization: Bearer $HTOK")" 'scene?v='
check "非成员读场景 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$SCROOM/scene" -H "Authorization: Bearer $ATOK")" "403"
printf '{"asset":{"version":"2.0"}}' > "$TMP/room.gltf"
check ".gltf（JSON）上传 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$SCROOM/scene" -H "Authorization: Bearer $HTOK" \
  -F "file=@$TMP/room.gltf")" "400"
mk_glb "$TMP/big.glb" 52430000
check "超 50MB 场景 413" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$SCROOM/scene" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/big.glb")" "413"
check "DELETE 清场景" "$(curl -sS -X DELETE "$URL/api/rooms/$SCROOM/scene" -H "Authorization: Bearer $HTOK")" '"scene":null'
check "清空后下载 404" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$SCROOM/scene" -H "Authorization: Bearer $HTOK")" "404"

echo "== 共同文件 =="
FROOM="e2e-files-$SUF"
python3 -c 'import json,sys;print(json.dumps({"roomName":sys.argv[1],"visibility":"public"}))' "$FROOM" > "$TMP/f_join.json"
curl -sS "$URL/api/rooms" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d @"$TMP/f_join.json" >/dev/null
curl -sS "$URL/api/rooms" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d @"$TMP/f_join.json" >/dev/null
check "初始文件列表 revision 0" "$(curl -sS "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK")" '"revision":0'
check "房间详情含 files 概要" "$(curl -sS "$URL/api/rooms/$FROOM" -H "Authorization: Bearer $ATOK")" '"files":{"revision":0,"locked":false,"canEdit":true,"count":0}'
check "房间详情 myPermissions 含 canEditFiles" "$(curl -sS "$URL/api/rooms/$FROOM" -H "Authorization: Bearer $ATOK")" '"canEditFiles":true'
check "非成员读文件列表 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $OTOK")" "403"
# 长轮询先挂起，等下面的创建唤醒它
( curl -sS "$URL/api/rooms/$FROOM/files?sinceRevision=0&wait=5" -H "Authorization: Bearer $ATOK" > "$TMP/lp.json" ) &
LP=$!
sleep 1
CREATE_OUT=$(curl -sS -X POST "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  -d '{"name":"会议纪要.md","content":"# 第一轮纪要","description":"纪要 Agent 维护"}')
check "JSON 直写 markdown" "$CREATE_OUT" '"kind":"markdown"'
wait $LP || true
check "长轮询被文件变更唤醒" "$(cat "$TMP/lp.json")" "会议纪要"
check "重名创建 409" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  -d '{"name":"会议纪要.md","content":"x"}')" "409"
check "二进制扩展名 JSON 直写 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  -d '{"name":"pic.png","content":"x"}')" "400"
mk_glb "$TMP/f.glb"
check "multipart 上传 GLB" "$(curl -sS -X POST "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $HTOK" \
  -F "file=@$TMP/f.glb;filename=样机.glb" -F "description=评审用样机")" '"kind":"model"'
FLIST=$(curl -sS "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK")
check "列表 revision=2" "$FLIST" '"revision":2'
FID1=$(printf '%s' "$FLIST" | python3 -c "import sys,json;print([f['id'] for f in json.load(sys.stdin)['files'] if f['kind']=='markdown'][0])")
FID2=$(printf '%s' "$FLIST" | python3 -c "import sys,json;print([f['id'] for f in json.load(sys.stdin)['files'] if f['kind']=='model'][0])")
check "未摆放 model 的 world 为 null" "$(printf '%s' "$FLIST" | python3 -c "import sys,json;print(all(f['world'] is None for f in json.load(sys.stdin)['files']))")" "True"
check "房主下载 md 内容" "$(curl -sS "$URL/api/rooms/$FROOM/files/$FID1/content" -H "Authorization: Bearer $HTOK")" "第一轮纪要"
check "单文件 GET 元数据" "$(curl -sS "$URL/api/rooms/$FROOM/files/$FID1" -H "Authorization: Bearer $HTOK")" '"file":{"id":'"$FID1"',"name":"会议纪要.md"'
check "内容 inline 带 Content-Type" "$(curl -sS -o /dev/null -w '%{content_type}' "$URL/api/rooms/$FROOM/files/$FID1/content" -H "Authorization: Bearer $HTOK")" "text/markdown"
check "?download=1 改 attachment" "$(curl -sS -D - -o /dev/null "$URL/api/rooms/$FROOM/files/$FID1/content?download=1" -H "Authorization: Bearer $HTOK" | tr -d '\r')" "attachment"
PUT_OUT=$(curl -sS -X PUT "$URL/api/rooms/$FROOM/files/$FID1" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"content":"# 第二轮纪要"}')
check "PUT 替换（LWW）" "$(curl -sS "$URL/api/rooms/$FROOM/files/$FID1/content" -H "Authorization: Bearer $HTOK")" "第二轮纪要"
check "替换后 revision=3" "$PUT_OUT" '"revision":3'
check "旧 baseUpdatedAt 409" "$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$URL/api/rooms/$FROOM/files/$FID1" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d '{"content":"x","baseUpdatedAt":"2000-01-01 00:00:00.000"}')" "409"
NEW_BASE=$(printf '%s' "$PUT_OUT" | J "['file']['updatedAt']")
check "新 baseUpdatedAt 200" "$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$URL/api/rooms/$FROOM/files/$FID1" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"content":"# 第三轮纪要","baseUpdatedAt":sys.argv[1]}))' "$NEW_BASE")")" "200"
printf 'plain text v4\n' > "$TMP/a.txt"
check "multipart 替换（kind 随存量名保持 markdown）" "$(curl -sS -X PUT "$URL/api/rooms/$FROOM/files/$FID1" -H "Authorization: Bearer $ATOK" \
  -F "file=@$TMP/a.txt")" '"kind":"markdown"'
curl -sS -X POST "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"name":"草稿.txt","content":"草稿"}' >/dev/null
FID3=$(curl -sS "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK" | python3 -c "import sys,json;print([f['id'] for f in json.load(sys.stdin)['files'] if f['name']=='草稿.txt'][0])")
check "PATCH 改名重分类 txt→md" "$(curl -sS -X PATCH "$URL/api/rooms/$FROOM/files/$FID3" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"name":"草稿.md"}')" '"kind":"markdown"'
check "改名撞名 409" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$FROOM/files/$FID3" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"name":"会议纪要.md"}')" "409"
check "PATCH 空字段 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$FROOM/files/$FID3" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{}')" "400"
check "单文件元数据 404" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$FROOM/files/999999" -H "Authorization: Bearer $ATOK")" "404"
check "DELETE 文件" "$(curl -sS -X DELETE "$URL/api/rooms/$FROOM/files/$FID3" -H "Authorization: Bearer $ATOK")" '"deleted"'
check "二次删除 404" "$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$URL/api/rooms/$FROOM/files/$FID3" -H "Authorization: Bearer $ATOK")" "404"
curl -sS -X PUT "$URL/api/rooms/$FROOM/permissions/$AGENT" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"canEditFiles": false}' >/dev/null
check "canEditFiles=0 后创建 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"name":"x.md","content":"x"}')" "403"
check "canEditFiles=0 后删除仍 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$URL/api/rooms/$FROOM/files/$FID1" -H "Authorization: Bearer $ATOK")" "403"
check "编辑权不影响读取" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK")" "200"
curl -sS -X PUT "$URL/api/rooms/$FROOM/permissions/$AGENT" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"canEditFiles": true}' >/dev/null
REV_LOCKED=$(curl -sS "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK" | J "['revision']")
curl -sS -X PATCH "$URL/api/rooms/$FROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"filesLocked": true}' >/dev/null
check "filesLocked 后成员创建 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d '{"name":"y.md","content":"y"}')" "403"
check "filesLocked 后房主仍可写" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$FROOM/files/$FID1" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"description":"锁定态房主可改"}')" "200"
check "filesLocked 变更递增 revision" "$(curl -sS "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK" | J "['revision']")" "$(python3 -c "print($REV_LOCKED + 2)")"
curl -sS -X PATCH "$URL/api/rooms/$FROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"filesLocked": false}' >/dev/null

echo "== 共同文件 3D 摆放（需求 10）=="
for i in 1 2 3 4 5 6 7; do
  mk_glb "$TMP/m$i.glb"
  curl -sS -X POST "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/m$i.glb" -F "name=m$i.glb" >/dev/null
done
MIDS=$(curl -sS "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $HTOK" | python3 -c "import sys,json;ids=[f['id'] for f in json.load(sys.stdin)['files'] if f['kind']=='model'];print(' '.join(str(i) for i in sorted(ids)))")
PLACE() { # PLACE <fileId> <body> → http 码
  curl -sS -o /dev/null -w '%{http_code}' -X PUT "$URL/api/rooms/$FROOM/files/$1/placement" -H "Authorization: Bearer ${3:-$HTOK}" -H 'Content-Type: application/json' -d "$2"
}
i=0
for mid in $MIDS; do
  i=$((i+1))
  [ $i -gt 6 ] && break
  BODY=$(printf '{"visible":true,"position":[%s.0,0.0,-2.5],"rotation":[0.0,3.14,0.0],"scale":[1.2,1.2,1.2]}' "$i")
  check "摆入第 $i 个 200" "$(PLACE "$mid" "$BODY")" "200"
done
M7=$(printf '%s' "$MIDS" | awk '{print $7}')
check "第 7 个摆入 400" "$(PLACE "$M7" '{"visible":true,"position":[0.0,0.0,-1.0]}')" "400"
check "非 model 摆入 400" "$(PLACE "$FID1" '{"visible":true,"position":[0.0,0.0,-1.0]}')" "400"
check "摆入缺少 position 400" "$(PLACE "$M7" '{"visible":true}')" "400"
check "position 非三元组 400" "$(PLACE "$M7" '{"visible":true,"position":[1,2]}')" "400"
check "列表返回 world 位姿（样机.glb）" "$(curl -sS "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK" | python3 -c "
import sys,json
f=[x for x in json.load(sys.stdin)['files'] if x['name']=='样机.glb'][0]
w=f['world']
ok=w and w['visible'] and w['pose']['position']==[1.0,0.0,-2.5] and w['pose']['rotation']==[0.0,3.14,0.0] and w['pose']['scale']==[1.2,1.2,1.2] and w['updatedBy']
print('OK' if ok else 'BAD:'+json.dumps(w))")" "OK"
check "world 带更新者" "$(curl -sS "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $ATOK")" '"updatedBy"'
curl -sS -X PUT "$URL/api/rooms/$FROOM/permissions/$AGENT" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"canEditFiles": false}' >/dev/null
check "无编辑权摆入 403" "$(PLACE "$M7" '{"visible":true,"position":[0.0,0.0,-1.0]}' "$ATOK")" "403"
curl -sS -X PATCH "$URL/api/rooms/$FROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"filesLocked": true}' >/dev/null
check "filesLocked 下摆入 403" "$(PLACE "$M7" '{"visible":true,"position":[0.0,0.0,-1.0]}' "$ATOK")" "403"
curl -sS -X PATCH "$URL/api/rooms/$FROOM" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"filesLocked": false}' >/dev/null
curl -sS -X PUT "$URL/api/rooms/$FROOM/permissions/$AGENT" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"canEditFiles": true}' >/dev/null
M6=$(printf '%s' "$MIDS" | awk '{print $6}')
CLOSE_OUT=$(curl -sS -X PUT "$URL/api/rooms/$FROOM/files/$M6/placement" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"visible":false}')
check "关闭后 visible false 保留位姿" "$CLOSE_OUT" '"visible":false'
check "关闭后 pose 仍在" "$CLOSE_OUT" '"position":[6.0,0.0,-2.5]'
check "腾位后第 7 个可摆入" "$(PLACE "$M7" '{"visible":true,"position":[0.0,0.0,-1.0]}')" "200"

echo "== 共同文件归档 =="
ARCHF=$(curl -sS -X POST "$URL/api/rooms/$FROOM/archive" -H "Authorization: Bearer $HTOK" | J "['roomId']")
check "归档后房间文件接口 404（房间名不可再寻址）" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/rooms/$FROOM/files" -H "Authorization: Bearer $HTOK")" "404"
ARCH_FILES=$(curl -sS "$URL/api/archives/$ARCHF/files" -H "Authorization: Bearer $HTOK")
check "归档文件列表可读" "$ARCH_FILES" "会议纪要"
check "归档文件列表 readOnly" "$ARCH_FILES" '"readOnly":true'
check "归档文件 world 只读可见" "$(printf '%s' "$ARCH_FILES" | python3 -c "import sys,json;print(sum(1 for f in json.load(sys.stdin)['files'] if f['world'] and f['world']['visible']))")" "6"
check "归档文件内容可下载" "$(curl -sS "$URL/api/archives/$ARCHF/files/$FID1/content" -H "Authorization: Bearer $HTOK")" "plain text v4"
check "外人读归档文件 403" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/archives/$ARCHF/files" -H "Authorization: Bearer $OTOK")" "403"

echo "== 内置缺省 3D 形象 =="
check "内置形象目录含 robert" "$(curl -sS "$URL/api/avatar-models" -H "Authorization: Bearer $HTOK")" '"id":"robert"'
check "内置形象共 100 个" "$(curl -sS "$URL/api/avatar-models" -H "Authorization: Bearer $HTOK" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["avatars"]))')" "100"
check "内置形象目录含新增项 fungus" "$(curl -sS "$URL/api/avatar-models" -H "Authorization: Bearer $HTOK")" '"id":"fungus"'
check "内置形象目录含末位 pumpkin" "$(curl -sS "$URL/api/avatar-models" -H "Authorization: Bearer $HTOK")" '"id":"pumpkin"'
check "新增形象的静态 VRM 可访问" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/static/avatars/fungus.vrm")" "200"
check "新增形象的缩略图可访问" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/static/avatars/pumpkin.webp")" "200"
check "选用新增的内置形象" "$(curl -sS -X PUT "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"url":"builtin:fungus"}')" '"model3dUrl":"builtin:fungus"'
check "未登录拿形象目录 401" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/avatar-models")" "401"
check "内置静态 VRM 可访问" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/static/avatars/astronaut.vrm")" "200"
check "内置缩略图可访问" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/static/avatars/astronaut.webp")" "200"
check "形象来源凭证可访问" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/static/avatars/CREDITS.md")" "200"
check "选用内置形象" "$(curl -sS -X PUT "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"url":"builtin:robert"}')" '"model3dUrl":"builtin:robert"'
check "内置形象带表情与骨骼能力位" "$(curl -sS "$URL/api/me" -H "Authorization: Bearer $HTOK")" '"model3dHumanoid":true'
check "未知内置 id 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"url":"builtin:ghost-x"}')" "400"
check "外链形象仍可用（回归）" "$(curl -sS -X PUT "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"url":"https://example.com/a.vrm"}')" '"model3dUrl":"https://example.com/a.vrm"'
check "上传自己的模型覆盖内置" "$(curl -sS -X POST "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK" -F "file=@$TMP/room.glb")" '"model3dUrl":"/api/users/'
check "清除形象" "$(curl -sS -X DELETE "$URL/api/me/model3d" -H "Authorization: Bearer $HTOK")" '"model3dUrl":null'
AVAGENT="e2e-avagent-$SUF"
check "Agent 创建时选内置形象" "$(curl -sS -X POST "$URL/api/agents" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1],"publicKey":open(sys.argv[2]).read(),"model3dUrl":"builtin:astronaut"}))' "$AVAGENT" "$TMP/pub.pem")")" '"model3dUrl":"builtin:astronaut"'
check "Agent 用未知内置 id 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/api/agents" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1],"publicKey":open(sys.argv[2]).read(),"model3dUrl":"builtin:nope"}))' "e2e-avbad-$SUF" "$TMP/pub.pem")")" "400"

echo "== 语音文本补写（v2.21）=="
mk_wav() { # 最小 WAV（RIFF/WAVE 魔数即可过音频校验），44 字节头 + 8 字节静音
  python3 -c 'import struct,sys
open(sys.argv[1],"wb").write(
  b"RIFF"+struct.pack("<I",44)+b"WAVE"
  +b"fmt "+struct.pack("<IHHIIHH",16,1,1,8000,8000,1,8)
  +b"data"+struct.pack("<I",8)+b"\x00"*8)' "$1"
}
mk_wav "$TMP/v1.wav"
VA=$(curl -sS -X POST "$URL/api/rooms/$PUB_ROOM/voice" -H "Authorization: Bearer $HTOK" \
  -F "file=@$TMP/v1.wav;filename=voice.webm" -F "text=")
check "主人发空文本语音" "$VA" '"content":"","msgType":"voice",'
VIDA=$(printf '%s' "$VA" | J "['id']")
check "Agent 补写识别文本 200" "$(curl -sS -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$VIDA/text" -H "Authorization: Bearer $ATOK" \
  -H 'Content-Type: application/json' -d '{"text":"你好"}')" '"content":"你好"'
check "补写后 streamIds 能拉到更新" "$(curl -sS "$URL/api/rooms/$PUB_ROOM/messages?afterId=$VIDA&streamIds=$VIDA" -H "Authorization: Bearer $HTOK")" "你好"
check "已有正文再补 409" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$VIDA/text" -H "Authorization: Bearer $ATOK" \
  -H 'Content-Type: application/json' -d '{"text":"别的"}')" "409"
mk_wav "$TMP/v2.wav"
VB=$(curl -sS -X POST "$URL/api/rooms/$PUB_ROOM/voice" -H "Authorization: Bearer $HTOK" \
  -F "file=@$TMP/v2.wav;filename=voice.webm" -F "text=")
VIDB=$(printf '%s' "$VB" | J "['id']")
check "Agent 写占位（空）200" "$(curl -sS -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$VIDB/text" -H "Authorization: Bearer $ATOK" \
  -H 'Content-Type: application/json' -d '{"text":"（空）"}')" '"content":"（空）"'
check "作者本人覆盖占位 200" "$(curl -sS -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$VIDB/text" -H "Authorization: Bearer $HTOK" \
  -H 'Content-Type: application/json' -d '{"text":"我自己补"}')" '"content":"我自己补"'
curl -sS "$URL/api/rooms" -H "Authorization: Bearer $OTOK" -H 'Content-Type: application/json' -d @"$TMP/pub_join.json" >/dev/null
mk_wav "$TMP/v3.wav"
curl -sS -X POST "$URL/api/rooms/$PUB_ROOM/voice" -H "Authorization: Bearer $OTOK" \
  -F "file=@$TMP/v3.wav;filename=voice.webm" -F "text=" >/dev/null
VIDC=$(curl -sS "$URL/api/rooms/$PUB_ROOM/messages" -H "Authorization: Bearer $HTOK" \
  | python3 -c "import sys,json;print([m['id'] for m in json.load(sys.stdin)['messages'] if m['msgType']=='voice' and m['username']=='$OTHER'][0])")
check "他人（房主）补写别人语音 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$VIDC/text" -H "Authorization: Bearer $HTOK" \
  -H 'Content-Type: application/json' -d '{"text":"x"}')" "403"
check "主人名下 Agent 补写外人语音 403" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$VIDC/text" -H "Authorization: Bearer $ATOK" \
  -H 'Content-Type: application/json' -d '{"text":"x"}')" "403"
check "补写带 @@ 前缀 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$VIDC/text" -H "Authorization: Bearer $OTOK" \
  -H 'Content-Type: application/json' -d '{"text":"@@x hi"}')" "400"
check "补写带 # 前缀 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$VIDC/text" -H "Authorization: Bearer $OTOK" \
  -H 'Content-Type: application/json' -d '{"text":"#g hi"}')" "400"
TXTID=$(curl -sS "$URL/api/rooms/$PUB_ROOM/messages" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  -d '{"content":"普通文本"}' | J "['id']")
check "对文本消息补写 400" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$TXTID/text" -H "Authorization: Bearer $ATOK" \
  -H 'Content-Type: application/json' -d '{"text":"x"}')" "400"
check "空文本 body 422" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$VIDC/text" -H "Authorization: Bearer $OTOK" \
  -H 'Content-Type: application/json' -d '{"text":""}')" "422"
check "纯空白文本 422" "$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$VIDC/text" -H "Authorization: Bearer $OTOK" \
  -H 'Content-Type: application/json' -d '{"text":"   "}')" "422"
check "作者本人补写空文本 200" "$(curl -sS -X PATCH "$URL/api/rooms/$PUB_ROOM/voice/$VIDC/text" -H "Authorization: Bearer $OTOK" \
  -H 'Content-Type: application/json' -d '{"text":"作者自己"}')" '"content":"作者自己"'

echo "== Agent 停用 =="
curl -sS -X PATCH "$URL/api/agents/$AGENT" -H "Authorization: Bearer $HTOK" -H 'Content-Type: application/json' -d '{"status":"disabled"}' >/dev/null
check "停用后取挑战 401" "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/agent-auth/challenge" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1]}))' "$AGENT")")" "401"

echo
echo "通过 $PASS 项，失败 $FAIL 项"
[[ $FAIL -eq 0 ]]
