#!/bin/bash
# gsv.cpp 语音服务停止 — VCPChat语音窗口关闭时自动调用 (Codex 2026-11-07)
for port in 9880 9881 9882; do
  for pid in $(lsof -ti :$port 2>/dev/null); do kill -9 $pid 2>/dev/null; done
done
echo "[gsv] services stopped"
