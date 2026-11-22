#!/bin/bash
# gsv.cpp 语音服务启动(幂等版) — VCPChat语音窗口自动拉起 (Codex 2026-11-07)
# 三端口全在则直接退出(幂等);否则全量重启
if lsof -ti :9880 >/dev/null 2>&1 && lsof -ti :9881 >/dev/null 2>&1 && lsof -ti :9882 >/dev/null 2>&1; then
    echo "[gsv] services already up, skip"
    exit 0
fi
GSV=/Users/raoye/Desktop/vcp/下载查看/gsv.cpp-macos验证/gsv.cpp
CERT=$("$GSV/.venv-sidecar/bin/python" -c "import certifi; print(certifi.where())")
for pid in $(lsof -ti :9882 2>/dev/null); do kill -9 $pid 2>/dev/null; done
for pid in $(lsof -ti :9880 2>/dev/null); do kill -9 $pid 2>/dev/null; done
for pid in $(lsof -ti :9881 2>/dev/null); do kill -9 $pid 2>/dev/null; done
sleep 1
# 1) 文本前端 sidecar
cd "$GSV" && GSV_G2PW_MODEL="$GSV/models/frontend/g2pw" \
  GSV_BERT_DIR="$GSV/models/frontend/bert_tokenizer" \
  NLTK_DATA="$GSV/models/frontend/.nltk_data" \
  SSL_CERT_FILE="$CERT" REQUESTS_CA_BUNDLE="$CERT" \
  https_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890 \
  nohup .venv-sidecar/bin/python tools/text_sidecar.py --port 9882 > /tmp/sidecar.log 2>&1 &
# 2) Metal推理引擎
cd "$GSV/build" && GSV_NO_FUSE=1 GSV_HUBERT_DIET=0 GSV_HUBERT_POS_GPU=0 \
  GSV_VOC_NO_FUSED=1 GSV_DIT_NO_FUSE_LN=1 \
  nohup ./gsv_server --models "$GSV/models" --device 0 \
  --port 9880 --sidecar 127.0.0.1:9882 \
  --allow-path "$GSV/tests/audio" --allow-path "$GSV/models" \
  --allow-path /Users/raoye/Desktop --allow-path /Users/raoye/Desktop/vcp/gsv_client_refs > /tmp/gsv_server.log 2>&1 &
# 3) VCPChat协议适配层
nohup node "$HOME/vcpchat/modules/voice/gsv-service/gsv_adapter.js" > /tmp/gsv_adapter.log 2>&1 &
echo "[gsv] services starting..."
