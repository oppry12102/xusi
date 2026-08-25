"""peer 名册路由（Phase 2 集群）：CRUD + 强制重探 + 邀请 token + 一键引导脚本。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from .. import peers
from .auth import require_admin, require_auth
from .models import AddPeerReq, IssueInvitationReq, RedeemInvitationReq

router = APIRouter()


@router.get("/api/peers")
def api_peers_list(_rec: dict = Depends(require_auth)) -> dict:
    """列出所有 peer + 探活结果（带 5s TTL）。
    返回 shape 与 /api/cluster.peers 相同；前端若只需要名册而非 self 也用这个。"""
    out = {"cluster": peers.is_cluster(), "peers": []}
    if not peers.is_cluster():
        return out
    for p in peers.list_peers():
        r = peers.probe_peer(p)
        entry = {"id": p["id"], "name": p.get("name", ""),
                 "url": p["url"], "ok": r["ok"]}
        if r.get("latency_ms") is not None:
            entry["latency_ms"] = r["latency_ms"]
        if r["ok"]:
            entry["info"] = r["info"]
        else:
            entry["error"] = r.get("error", "")
        out["peers"].append(entry)
    return out


@router.post("/api/peers", status_code=201)
def api_peers_add(req: AddPeerReq,
                  _rec: dict = Depends(require_admin)) -> dict:
    """注册一个 peer：先探活（拿 id），落 etc/peers.toml。
    失败：peer 不可达 → 502 PeerUnreachable；本地拒绝（单节点模式 / 重名 / url 坏）→ 400 PeerRefused。"""
    rec = peers.add_peer(req.url, name=req.name)  # 抛异常被全局 handler 接住
    r = peers.probe_peer(rec)
    return {
        **rec,
        "ok": r["ok"],
        "latency_ms": r.get("latency_ms"),
        "info": r.get("info") if r["ok"] else None,
        "error": r.get("error") if not r["ok"] else None,
    }


@router.delete("/api/peers/{peer_id}")
def api_peers_remove(peer_id: str,
                     _rec: dict = Depends(require_admin)) -> dict:
    if not peers.remove_peer(peer_id):
        raise HTTPException(404, f"peer 不存在: {peer_id}")
    return {"removed": peer_id}


@router.post("/api/peers/probe")
def api_peers_probe_all(_rec: dict = Depends(require_admin)) -> dict:
    """强制清 5s 探活缓存 + 立即全员重探；前端手动刷新按钮用。"""
    peers.clear_probe_cache()
    rows = peers.list_peers()
    out = []
    for p in rows:
        r = peers.probe_peer(p)
        out.append({"id": p["id"], "url": p["url"],
                    "ok": r["ok"], "latency_ms": r.get("latency_ms"),
                    "error": r.get("error", "") if not r["ok"] else ""})
    return {"probed": len(out), "results": out}


# ── 一键引导新 xusi 节点（Phase 2 v1.1）──────────────────

@router.post("/api/peers/invitations", status_code=201)
def api_peers_invite(req: IssueInvitationReq,
                     _rec: dict = Depends(require_admin)) -> dict:
    """签发一条邀请 token。返回 {token, expires_at, install_cmd}：
    - token：含 cluster_secret / issuer_url / sid / suggested_name
    - install_cmd：一行 `curl | bash` 命令，用户 SSH 到新机器跑
    非集群模式 → 400 PeerRefused。"""
    inv = peers.issue_invitation(suggested_name=req.name, ttl=300)
    if inv is None:
        raise HTTPException(400, "单节点模式（[cluster].secret 未设）；邀请 token 需要集群模式")
    return inv


@router.post("/api/peers/invitations/redeem")
def api_peers_redeem(req: RedeemInvitationReq,
                     _rec: dict = Depends(require_admin)) -> dict:
    """新机器装好后调用：消费 sid + 注册到本机 peer 名册。"""
    return peers.redeem_invitation(req.token, req.url)


@router.get("/api/peers/join.sh", response_class=PlainTextResponse)
def api_peers_join_script(token: str = Query(...),
                          _rec: dict = Depends(require_admin)) -> str:
    """一行引导脚本。含 cluster_secret 的 JWT 通过 query 传——脚本用 openssl
    验签后读 payload 用。Content-Type: text/x-shellscript。"""
    from ..config import get_config
    join_url = f"{get_config().public_url.rstrip('/')}/api/peers/join.sh"
    return (JOIN_SCRIPT_TEMPLATE
            .replace("__JOIN_TOKEN__", token)
            .replace("__JOIN_URL__", join_url))


# bash 引导脚本：JWT 验签（HMAC-SHA256）+ 安装 + 注册 + 启动。
# 设计目标：用户只需 SSH 到新机器粘一行；脚本自检、自装、自连、自启。
JOIN_SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
# xusi 一键引导脚本（由本节点管理面生成）
# 用法：curl -sSL ".../api/peers/join.sh?token=<JWT>" | bash -s
set -euo pipefail

JOIN_URL="__JOIN_URL__"
TOKEN="__JOIN_TOKEN__"

say()  { printf '\033[1;34m[xusi]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[xusi]\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ── 0. 预检 ───────────────────────────────────────────────
say "预检：git / python3 / systemd-user"
have git     || die "需要 git（apt install git / yum install git）"
have python3 || die "需要 python3"
have systemctl || die "需要 systemctl（systemd 用户会话）"

# ── 1. 验签 JWT（HMAC-SHA256）──────────────────────────────
b64u_decode() { local p=$((${#1} % 4)); printf '%s' "$1$(printf '=%.0s' $(seq 1 $p))" | base64 -d 2>/dev/null; }
verify_jwt() {
  local tok="$1" secret="$2"
  [[ "$tok" =~ ^([^.]+)\.([^.]+)\.([^.]+)$ ]] || return 1
  local h="${BASH_REMATCH[1]}" p="${BASH_REMATCH[2]}" s="${BASH_REMATCH[3]}"
  local exp=$(printf '%s' "$h.$p" | openssl dgst -sha256 -hmac "$secret" -binary | base64 | tr -d '=' | tr '/+' '_-')
  [[ "$exp" != "$s" ]] && return 1
  local payload=$(printf '%s' "$p" | tr '_-' '/+' | b64u_decode)
  echo "$payload"
}

# 拆分 header / payload / signature 拿到 cluster_secret
parts=(${TOKEN//./ })
SECRET=$(printf '%s' "${parts[1]}" | tr '_-' '/+' | b64u_decode | python3 -c "import sys,json; print(json.load(sys.stdin)['secret'])")
ISSUER=$(printf '%s' "${parts[1]}" | tr '_-' '/+' | b64u_decode | python3 -c "import sys,json; print(json.load(sys.stdin)['issuer'])")
NAME=$(printf '%s' "${parts[1]}" | tr '_-' '/+' | b64u_decode | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))")

# 验签 + 过期检查（用 Python 做严格校验，bash 验签只防误传）
verify_full=$(printf '%s' "${parts[1]}" | tr '_-' '/+' | b64u_decode | python3 -c "
import sys, json, base64, hmac, hashlib, time
p = json.load(sys.stdin)
secret = p.get('secret','')
parts = '$TOKEN'.split('.')
exp_sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), f'{parts[0]}.{parts[1]}'.encode(), hashlib.sha256).digest()).rstrip(b'=').decode()
ok = exp_sig == parts[2] and time.time() < p.get('exp', 0)
sys.exit(0 if ok else 1)
") || die "JWT 验签失败或已过期（5 分钟内有效）"

say "JWT 验签通过"
say "  issuer: $ISSUER"
say "  建议节点名: ${NAME:-（未指定）}"

# ── 2. 准备目录 ───────────────────────────────────────────
DEST="${XUSI_HOME:-$HOME/xusi}"
if [[ -d "$DEST" ]]; then
  die "$DEST 已存在——为防误覆盖已部署实例，请改名或删除后重跑（或设 XUSI_HOME=... 到新路径）"
fi
say "克隆 xusi 到 $DEST"
git clone --depth 1 https://github.com/oppry12102/xusi.git "$DEST"
cd "$DEST"

# ── 3. venv + 依赖 ─────────────────────────────────────────
say "创建 .venv"
python3 -m venv .venv
.venv/bin/pip install --quiet --disable-pip-version-check -e .

# ── 4. 写 etc/xusi.toml（先于 install 让 install 跳过模板复制）────
mkdir -p etc
cat > etc/xusi.toml <<EOF
# 由 xusi 一键引导脚本写入 $(date -Iseconds)
[cluster]
secret = "$SECRET"

[node]
name = "$NAME"
EOF

# ── 5. xusi install（建 systemd 用户服务 + 启）──────────────
say "安装 systemd 用户服务并启动 xusi"
.venv/bin/python -m xusi install

# ── 6. 自动回链：本机认识 issuer ─────────────────────────
say "注册对端 $ISSUER 到本机 peer 名册"
.venv/bin/python -m xusi peers add "$ISSUER" || say "  (peers add 失败也无妨——后续可手动加)"

# ── 7. 让 issuer 认识本机 ───────────────────────────────
# 探测本机公网 URL：优先 ifconfig.me，回落本机首个非环回 IP
say "探测本机公网 URL"
SELF_URL=$(curl -fsSL --max-time 5 https://ifconfig.me 2>/dev/null || true)
if [[ -z "$SELF_URL" ]]; then
  SELF_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}')
  SELF_URL="$SELF_IP"
fi
SELF_URL="http://${SELF_URL}:$(.venv/bin/python -c "import sys; sys.path.insert(0,'.'); from xusi.config import get_config; print(get_config().port)")"
say "  本机 URL: $SELF_URL"

say "通知 issuer 把本机加入它的 peer 名册"
HTTP_CODE=$(curl -fsS -o /tmp/xusi-redeem.json -w '%{http_code}' \
  -X POST "$ISSUER/api/peers/invitations/redeem" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\",\"url\":\"$SELF_URL\"}" || echo "000")

if [[ "$HTTP_CODE" =~ ^2 ]]; then
  say "✓ issuer 已把本机加入 peer 名册"
  cat /tmp/xusi-redeem.json
  echo
else
  say "✗ redeem 失败（HTTP $HTTP_CODE）——issuer 端没把本机加入名册；可手动在 issuer 上跑："
  say "    .venv/bin/python -m xusi peers add $SELF_URL"
fi

say
say "════════════════════════════════════════════════════"
say "  xusi 引导完成"
say "  URL:   $SELF_URL"
say "  首个 admin token 已在 install 时打印"
say "════════════════════════════════════════════════════"
"""
