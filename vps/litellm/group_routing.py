"""US-P13：把已鉴权 Key 的 provider 分组注入为本请求的路由 tag（网关侧、鉴权后）。

在 LiteLLM 1.96.2 实测校准的三点：
1. tag 过滤默认不开：路由器级 enable_tag_filtering 或请求级 kwargs.enable_tag_filtering
   二者其一为 True 才生效；本钩子对每个请求强制置 True（分组授权不依赖全局配置正确性）。
2. 带 "default" tag 的 deployment 会被实现当作「tag 无匹配时的兜底池」——与基线
   「组内无该模型部署 → 可判读错误，不误路由到组外」冲突。因此 deployment 一律
   不打 default tag：未绑组 Key 由本钩子清空 tags → 走全量池（default 组=全部 provider）。
3. 客户端可经 x-litellm-tags 头自带 tag（pre-auth 即并入 metadata.tags）——本钩子在
   鉴权后无条件覆写：绑组 Key 注入 [group]，未绑组 Key 清空，伪造 tag 既进不了组、
   也盖不掉网关的组 tag。
"""

from litellm.integrations.custom_logger import CustomLogger


def _get(d, key):
    """dict / 请求对象两种形态统一取参。"""
    if isinstance(d, dict):
        return d.get(key)
    return getattr(d, key, None)


def _effort(data) -> str:
    """归一化本请求实际携带的思考强度（网关不改写 effort，携带值即生效值）。

    OpenAI 协议 reasoning_effort / reasoning.effort → 档位原样（low/medium/high…）；
    Anthropic 协议 thinking.budget_tokens → "budget:N"；thinking.type=disabled → "off"；
    未携带 → 空串（不写键）。已知局限：钩子在路由前，drop_params 对不支持上游的
    静默丢弃不可感知，记录的是请求携带值。
    """
    re_ = _get(data, "reasoning_effort")
    if isinstance(re_, str) and re_:
        return re_
    reasoning = _get(data, "reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str) and reasoning["effort"]:
        return reasoning["effort"]
    thinking = _get(data, "thinking")
    if isinstance(thinking, dict):
        if isinstance(thinking.get("budget_tokens"), int):
            return f"budget:{thinking['budget_tokens']}"
        if thinking.get("type") == "disabled":
            return "off"
    return ""


def _set_effort(metadata: dict, data) -> None:
    """effort 原地写入 metadata（两路）。

    1.96.2 实测：① spend log 落库时按 SpendLogsMetadata.__annotations__ 白名单重建
    metadata，requester_metadata 不在白名单内会被丢弃；spend_logs_metadata（自由
    键值槽）在白名单内，是唯一可靠落库通道。② function_setup 在本钩子之前就把
    data["metadata"] 以同一引用存进 Logging 对象——必须**原地改写**，整体替换
    data["metadata"] 会让日志层握着旧引用、改动全部丢失。"""
    effort = _effort(data)
    if not effort:
        return
    sl = metadata.get("spend_logs_metadata")
    if not isinstance(sl, dict):
        sl = {}
        metadata["spend_logs_metadata"] = sl
    sl["effort"] = effort
    rm = metadata.get("requester_metadata")   # 非 DB 日志消费方（otel 等）仍读这里
    if not isinstance(rm, dict):
        rm = {}
        metadata["requester_metadata"] = rm
    rm["effort"] = effort


def _apply(group: str, data) -> None:
    if isinstance(data, dict):
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            data["metadata"] = metadata
        if group:
            metadata["tags"] = [group]
        else:
            metadata.pop("tags", None)  # 未绑组 = 全量池；顺带清除客户端伪造的 tag
        _set_effort(metadata, data)
        # /v1/messages 入口：代理把请求 metadata 放在 litellm_metadata（非 metadata），
        # function_setup 以同引用进日志层的 litellm_params["litellm_metadata"]
        lm = data.get("litellm_metadata")
        if isinstance(lm, dict):
            _set_effort(lm, data)
        # 直写 Logging 对象：实测（1.96.2）请求 data["metadata"] 在路由/主调用层会被
        # 重建，而 spend log 读的是 function_setup 存进 Logging 的那份 litellm_params
        # ——请求侧两通道 + Logging 对象三路同写，确保 effort 到达落库层。
        lo = data.get("litellm_logging_obj")
        lp = getattr(lo, "litellm_params", None)
        if isinstance(lp, dict):
            lmd = lp.get("metadata")
            if not isinstance(lmd, dict):
                lmd = {}
                lp["metadata"] = lmd
            _set_effort(lmd, data)
        data["enable_tag_filtering"] = True
    else:  # 部分入口传对象；尽力设置
        metadata = dict(getattr(data, "metadata", None) or {})
        if group:
            metadata["tags"] = [group]
        else:
            metadata.pop("tags", None)
        _set_effort(metadata, data)
        data.metadata = metadata
        try:
            data.enable_tag_filtering = True
        except (AttributeError, ValueError):
            pass


class GroupRoutingHook(CustomLogger):
    """async_pre_call_hook：鉴权后、路由前，Key 的 metadata.group → 本请求路由 tag。"""

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        metadata = getattr(user_api_key_dict, "metadata", None) or {}
        group = metadata.get("group") or ""
        if group == "default":  # default 组 = 全量池（隐式），不作为路由 tag
            group = ""
        _apply(group, data)
        return data


# LiteLLM callbacks 配置须指向实例而非类（类会被当实例调用，报 missing self）
group_routing_hook = GroupRoutingHook()
