"""
SdkError patch — 兼容 NEKO framework
"""

def patch_sdk_error():
    """修复 NEKO SdkError 缺少 .message 属性的问题
    
    NEKO framework 某些模块（如 market_bridge.py）会访问 err.message，
    但 SdkError 基类未定义此属性，导致 AttributeError。
    在插件启动时一次性 patch，对所有后续 SdkError 实例生效。
    """
    try:
        from plugin.sdk.plugin import SdkError

        if hasattr(SdkError, '__message_patched__'):
            return  # 已经 patch 过

        _orig_init = SdkError.__init__

        def _patched_init(self, message="", *args, **kwargs):
            _orig_init(self, message, *args, **kwargs)
            if not hasattr(self, 'message') or self.message is None:
                object.__setattr__(self, 'message', str(self.args[0]) if self.args else str(message))

        SdkError.__init__ = _patched_init
        SdkError.__message_patched__ = True
    except ImportError:
        pass  # NEKO SDK 不可用
