# Issue #162：模型生成乱码的独立复现

这份脱敏样本在验证环境中，只需续写 **8 个 token**，即可生成 UTF-8 字节被写成 Latin-1 字符的乱码箭头。输入本身没有该乱码特征，也没有 C1 控制字符。

它用于验证引擎生成的文字，不依赖 DSH、Portal 数据库或网关的转码修复。样本中的 shell 命令和工具调用是冻结的上下文文本；复现器不会执行它们。

## 1. 确认引擎与权重

已验证的基线如下；完整版本与指纹见 `manifest.json`。

| 项目 | 基线 |
| --- | --- |
| 硬件 | 2 × NVIDIA GB10 / DGX Spark |
| TensorFold | 0.6.0，基础提交 `c4646171139ee8a3c38103eaa1699dad226ec12b` |
| 部署包 | MiaAI v1.5，参考提交 `1576746` |
| 验证镜像 | 本地构建 `tensorfold-glm53:v0.6.0-miav15`，补丁指纹 `423ee93198b1` |
| 权重 | `Mia-AiLab/GLM-5.3-Flash-EXL3-4bpw-TensorFold` |
| 权重 revision | `078455ffe6472f9a52fbc1139f58b9db2881b25c` |
| 起草器 | `incoai/GLM-5.3-Flash-DFlash2@7d74cdd881ed7e32c31175984a67823127b66cfe` |
| 参数 | TP=2、parallel=8、context=1048576、DENSE=q4、KV=fp8 |
| 服务默认 | thinking 开启，clear_thinking=false，top_k=20，top_p=0.95 |

模型服务的搭建方式参见 [MiaAI 部署仓库](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold/tree/1576746)。验证镜像还包含单独发送 SSE usage 的本地补丁，不应把这里的结果表述成已经在所有官方镜像上验证。此客户端复现包不启动、停止或升级服务。

请直接连接模型引擎。经过 Portal 时，PR #163 的自动修补可能把故障掩盖。

## 2. 检查完整输入

文件均位于本目录：

- `prompt.txt`：方便人工审阅的完整明文输入。
- `request.json`：完整 OpenAI-compatible `/v1/completions` 请求。
- `manifest.json`：文件和输入 token 哈希、环境、预期错误 token。
- `reproduce.py`：仅依赖 Python 标准库的运行脚本，Python 3.10+。
- `observed-results.json`：本样本的实际运行结果。
- `LICENSE.Strata`：样本中公开 Strata 文档/代码片段的 MIT 许可。

先执行本地校验，不发出网络请求：

```bash
python3 tools/repro/issue-162/reproduce.py --check
```

应得到 `status: verified`、`recorded_input_tokens: 19438`。

最终样本为 **19,438 个输入 token、66,462 个字符**，不是之前私有原始探针的 19,848 token。标识替换和目录清单移除后已重新实测；未声称这是严格最小化的样本。

`prompt.txt` 保留了上下文中的三处行尾空格，以确保与已验证的请求逐字一致；请勿自动清理空白后继续沿用本样本的哈希和结果。

## 3. 单请求复现

在可以访问引擎的机器上运行。以下以引擎在本机 8890 端口为例：

```bash
python3 tools/repro/issue-162/reproduce.py \
  --base-url http://127.0.0.1:8890/v1
```

如果使用 SSH 转发，在另一个终端建立通道，再执行上面的命令：

```bash
ssh -N -L 8890:127.0.0.1:8890 YOUR_ENGINE_HOST
```

脚本默认使用 fixture 中的模型名 `GLM-5.3-Flash-EXL3`。如服务端的公开名称不同，可传入 `--model`。服务若需要鉴权，从当前进程的 `TENSORFOLD_API_KEY` 环境变量读取；不要把真实密钥写进样本或命令行参数。

也可以在无需鉴权的引擎本机直接发送完整请求：

```bash
curl --fail-with-body --max-time 180 \
  http://127.0.0.1:8890/v1/completions \
  -H 'Content-Type: application/json' \
  --data-binary @tools/repro/issue-162/request.json
```

请求已经包含渲染后的正常历史和正常续写前缀，使用 `add_special_tokens=false`，不需要再次套 chat template。没有把乱码答案放进输入要求模型照抄。

## 4. 观察结果

基线的错误输出用 JSON 转义表示为：

```json
" \u00e2\u0086\u0092 IQ3_S or"
```

前三个非 ASCII 字符分别是 `U+00E2 U+0086 U+0092`；文本应使用真正的箭头 `→`（`U+2192`）。这次观察到的实际输出 token ID 为：

```json
[27810, 126, 228, 74558, 36520, 18, 1098, 476]
```

输出 token 的 SHA-256（unsigned 32-bit little-endian）为：

```text
d64bee895f4499767a5abcc18f3bb41ea89d884b1a68fa03d5d49aa2a54ecde5
```

脚本以 JSON 输出文字、码点、乱码特征数、C1 控制字符数、usage、token ID 及哈希：

- 退出码 **1**：观察到乱码特征，成功复现故障。
- 退出码 **0**：本次没有观察到该特征；不代表所有请求已修复。
- 退出码 **2**：样本校验、网络、API、SSE 完整性或输入 token 数校验失败，本次不能直接比较。

`finish_reason=length` 是预期的：本探针有意只允许生成 8 个 token，乱码在这 8 个 token 内已出现。

## 5. 协议与起草器对照

```bash
# 同一输入连续重放两次
python3 tools/repro/issue-162/reproduce.py --repeat 2

# 流式响应
python3 tools/repro/issue-162/reproduce.py --stream

# 关闭 DFlash2 起草
python3 tools/repro/issue-162/reproduce.py --no-draft

# 温度 1，保持固定 seed
python3 tools/repro/issue-162/reproduce.py --temperature 1

# 可选：同时发起四个请求；用于一致性观察，不是吞吐基准
python3 tools/repro/issue-162/reproduce.py --repeat 4 --concurrency 4
```

在基线上，非流式两次、流式一次、关闭起草一次、温度 1 一次，共 **5/5** 生成了上述同一组错误 token。关闭起草那次缓存命中为 0。实测结果见 `observed-results.json`。

## 数据范围与来源

发布候选仅包含这份脱敏、重新验证的样本。原始长 DSH 会话含有敏感运维信息，不包含在本目录中。

主机别名、账号、用户路径、真实服务名称及非示例网络地址已替换；无关的主目录文件清单已移除，工具调用标识和媒体标识已替换。流式响应先拼接，再对续写前缀脱敏，避免路径跨多个 delta 时漏检。最后检查的是完整渲染后的输入文本。

其中 `example-host-via-relay`、`example-workstation`、`operator`、`demo-*` 及 `192.0.2.10` 为示例标识，`127.0.0.1` 和 `0.0.0.0` 为通用回环/绑定地址。通用硬件、软件版本及模型名称作为测试场景保留。脚本没有读取私有会话数据库的逻辑。

公开资料来自 [Niko1221/Strata](https://github.com/Niko1221/Strata)，参考版本 [6f32ec0](https://github.com/Niko1221/Strata/tree/6f32ec070f23ced9f50e704d854d775da52591ab)，涉及 README、模型/安装说明和 setup 片段；对应 MIT 版权及许可见 `LICENSE.Strata`。样本中的旧助手推断和操作叙述仅是冻结上下文，不是部署建议。

## 用于新旧版本比较

同权重 revision、DENSE/KV、模板、采样条件下比较引擎版本，并记录实际镜像及补丁集合。保留的 TF 0.5.0 最多支持 4 并发；做严格 A/B 时两版统一到 4。本文件中的原始错误基线是 parallel=8。

输入 token 数或 tokenizer 不一致时先定位差异。升级后只观察 Portal 的正常输出、或某次短请求没有乱码，都不足以证明推理端已修复。
