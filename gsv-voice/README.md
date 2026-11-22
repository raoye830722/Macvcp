# gsv-voice — GPT-SoVITS 语音克隆 macOS 化

gsv.cpp 推理引擎的 macOS (Apple Silicon) 编译适配 + VCPChat 协议适配层。零训练克隆：提供参考音频与对应文字即可合成。

## 上游
- 引擎：https://github.com/KakaruHayate/gsv.cpp （本地基线 commit 1716bb0）
- 适配层：VCPChat `modules/voice/gsv-service/`
- 模型：huggingface.co/KakaruHayate/GSV.CPP（体积大，单独下载，不入库）

## macOS 改造点

### 引擎补丁（engine-patches/0001-macos-metal-adaptation.patch）
- CMakeLists.txt：GGML Vulkan → 原生 Metal
- gsv_dit.cpp：GSV_DIT_NO_FUSE_LN 开关，Metal 路径 LayerNorm 非融合分解
- gsv_hubert.cpp / gsv_vocoder_ggml.cpp：Metal 路径算子适配
- 另有 2 个前端 md5 校验文件随构建再生成（含在 patch 中）

### 协议适配层（adapter/）
- gsv_adapter.js：VCPChat Sovits5 客户端 ↔ gsv_server 协议适配（9881↔9880），STABLE_SEED=42 稳定种子 + 20ms 尾部 micro-fade（压平 AR 采样尾部伪影）
- denoise_worker.py：v3 降噪链
- start_gsv.sh / stop_gsv.sh：三端口（9880 引擎 / 9881 适配 / 9882 文本前端）幂等启停

## 构建（Apple Silicon）
1. clone 上游并 `git apply` 本补丁；按上游 Releases 应用累积官方补丁
2. CMake 构建（Metal 后端）；模型从 HuggingFace 下载至 models/
3. 文本前端 sidecar 需 Python venv（g2pw/bert/nltk 资源）
4. start_gsv.sh 内的本地路径与代理端口需按自身环境调整

## 与 VCPChat 集成
语音窗口经 modules/SovitsTTS.js + ipc/sovitsHandlers.js 调用适配层（随 VCPChat 上游分发）；MimoDubbing 插件 engine=sovits 走 gsv:<预设ID> 音色协议；参考音频目录须为纯 ASCII 路径（server 白名单要求）。参考音频与预设属个人数据，不在本仓库。
