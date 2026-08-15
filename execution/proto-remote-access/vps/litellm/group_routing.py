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


def _apply(group: str, data) -> None:
    if isinstance(data, dict):
        metadata = dict(data.get("metadata") or {})
        if group:
            metadata["tags"] = [group]
        else:
            metadata.pop("tags", None)  # 未绑组 = 全量池；顺带清除客户端伪造的 tag
        data["metadata"] = metadata
        data["enable_tag_filtering"] = True
    else:  # 部分入口（如 /v1/messages）传对象；尽力设置
        metadata = dict(getattr(data, "metadata", None) or {})
        if group:
            metadata["tags"] = [group]
        else:
            metadata.pop("tags", None)
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
